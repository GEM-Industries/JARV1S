from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from core.context import get_ctx, get_owner_id, get_timezone
from core.decorators import tool
from core.operations.events import publish_operations_changed
from core.operations.setups import SetupPatch, definition_pause_patch, patch_rule_lifecycle
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.read_evidence import MatchStatus, ReadCoverage, match_status_from_count
from core.plugins.result import ToolResult
from core.plugins.ui import content_envelope, receipt_envelope
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.triggers.vocabulary import DECISION_ACT, DECISION_OFFER, DECISION_TELL, TriggerDecision
from core.triggers.delivery_policy import with_target_fallback_for_critical
from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    DeliveryTargetHint,
    FreshnessPolicy,
    ManagementOwnership,
    TriggerAction,
    TriggerOrigin,
)
from core.triggers.lifecycle import is_scheduler_managed, is_scheduler_managed_instance
from core.triggers.priority import SOUND_BY_LEVEL
from core.triggers.presets import (
    alarm_preset,
    deferred_instruction_preset,
    reminder_preset,
    timer_preset,
)
from core.triggers.service import trigger_service
from core.scheduling import (
    coerce_datetime,
    coerce_timezone,
    format_local_when,
    is_valid,
    local_datetime_fields,
    next_occurrence,
    parse_date,
    parse_schedule_time,
    recurrence_rule_from_origin,
)
from core.time import normalize_clock_time
from plugins.smart_home.node_binding import list_bound_room_names, resolve_location_ref_for_area_name
from services.database.mongodb import mongodb


PENDING_EDIT_STATUSES = ("pending", "awaiting_delivery")


class AlertSummary(BaseModel):
    instance_id: str | None = None
    series_id: str | None = None
    scope: Literal["instance", "series"]
    name: str = ""
    kind: Literal["timer", "alarm", "reminder"] | None = None
    sound: str = "chime"
    requires_ack: bool = False
    origin_kind: str | None = None
    level: str = "normal"
    message: str = ""
    status: str
    delivery_target: str = "anywhere"
    delivery_target_kind: str = "follow_me"
    time: str | None = None
    local_time: str | None = None
    local_date: str | None = None
    seconds_remaining: int | None = None
    minutes_remaining: int | None = None
    recurrence: str | None = None
    scheduled_local_time: str | None = None
    instructions: str | None = None


class AlertQueryResult(BaseModel):
    alerts: list[AlertSummary] = Field(default_factory=list)
    match_status: MatchStatus
    coverage: ReadCoverage = ReadCoverage.COMPLETE
    kind: Literal["timer", "alarm", "reminder"] | None = None
    query: str | None = None


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


def _parse_future_when(
    when: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> datetime:
    """Parse scheduler input into a future UTC datetime."""
    utc_now = now or datetime.now(timezone.utc)
    tz_name = timezone_name or get_timezone()
    return parse_schedule_time(when, now=utc_now, timezone_name=tz_name)


def _alert_kind(
    *,
    origin_kind: str | None,
    requires_ack: bool,
    decision: str | None = None,
) -> Literal["timer", "alarm", "reminder"] | None:
    if decision == DECISION_ACT:
        return None
    if origin_kind == "interval":
        return "timer"
    if requires_ack:
        return "alarm"
    return "reminder"


def _alert_list_entry(
    *,
    instance_id: str | None,
    series_id: str | None,
    scope: Literal["instance", "series"],
    name: str,
    due_at: datetime | None,
    action: dict[str, Any],
    attention: dict[str, Any],
    trigger: dict[str, Any],
    status: str,
    delivery_snapshot: dict[str, Any],
    now_utc: datetime,
) -> dict[str, Any]:
    origin_kind = trigger.get("kind")
    requires_ack = bool(attention.get("requires_ack"))
    sound = attention.get("sound", "chime")
    decision = action.get("decision")
    kind = _alert_kind(
        origin_kind=origin_kind,
        requires_ack=requires_ack,
        decision=decision,
    )

    entry: dict[str, Any] = {
        "instance_id": instance_id,
        "series_id": series_id,
        "scope": scope,
        "name": name,
        "kind": kind,
        "sound": sound,
        "requires_ack": requires_ack,
        "origin_kind": origin_kind,
        "level": attention.get("level", "normal"),
        "message": action.get("message", ""),
        "status": status,
        **_delivery_intent(delivery_snapshot),
    }
    instructions = (action.get("instructions") or "").strip()
    if instructions:
        entry["instructions"] = instructions

    if due_at is None:
        if trigger.get("recurrence"):
            entry["recurrence"] = trigger["recurrence"]
            entry["scheduled_local_time"] = trigger.get("original_local_time")
        return entry

    time_fields = local_datetime_fields(
        due_at,
        timezone_name=trigger.get("timezone") or get_timezone(),
        default=now_utc,
    )
    remaining_secs = max(0, int((due_at - now_utc).total_seconds()))
    entry.update(
        {
            **time_fields,
            "seconds_remaining": remaining_secs,
            "minutes_remaining": remaining_secs // 60,
        }
    )
    if trigger.get("recurrence"):
        entry["recurrence"] = trigger["recurrence"]
        entry["scheduled_local_time"] = (
            trigger.get("original_local_time")
            or due_at.astimezone(coerce_timezone(entry["timezone"])).strftime("%H:%M")
        )
    return entry


def _alert_matches_filters(
    entry: dict[str, Any],
    *,
    kind: str | None,
    status: str | None,
    recurrence: str | None,
    local_time: str | None,
    message: str | None,
) -> bool:
    if kind and entry.get("kind") != kind:
        return False
    if status and entry.get("status") != status:
        return False
    if recurrence and str(entry.get("recurrence", "")).casefold() != recurrence.casefold():
        return False
    if message:
        haystack = " ".join(
            str(entry.get(field, ""))
            for field in ("name", "message", "instructions")
            if entry.get(field)
        ).casefold()
        if message.casefold() not in haystack:
            return False
    if local_time:
        candidates = {
            normalize_clock_time(str(entry[field]))
            for field in ("scheduled_local_time", "local_time")
            if entry.get(field)
        }
        if local_time not in candidates:
            return False
    return True


def _one_scope_required_error(tool_name: str) -> CapabilityErrorDetail:
    return _fail(
        f"Name the target with query= (a name or clock time), or provide exactly one of "
        f"series_id or instance_id from get_alerts(). "
        f"For {tool_name}, query= or instance_id changes one pending occurrence; "
        "series_id is only for permanent/all-future recurring series changes."
    )


def _ambiguous_alerts_error(rows: list[AlertSummary]) -> CapabilityErrorDetail:
    labels = []
    for row in rows[:6]:
        label = row.name or row.kind or "item"
        when = row.local_time or row.scheduled_local_time or ""
        ident = row.instance_id or row.series_id or ""
        labels.append(f"{label} {when} ({ident})".strip())
    return _fail(
        "Multiple matches: "
        + "; ".join(labels)
        + ". Pass a more specific query, or instance_id/series_id."
    )


def _ids_from_resolved(
    resolved: AlertSummary, *, scope: Literal["occurrence", "series"] = "occurrence"
) -> tuple[str | None, str | None]:
    """Return (series_id, instance_id). Default to the pending occurrence."""
    if scope == "series":
        return resolved.series_id, None
    if resolved.instance_id:
        return None, resolved.instance_id
    return resolved.series_id, None


def _scheduler_manage_error(resource: str) -> CapabilityErrorDetail:
    return _fail(
        f"{resource} is not scheduler-managed. "
        "Use setups.find(query=...) to locate the owner and supported actions."
    )


async def _existing_named_series(owner_id: str, name: str) -> dict | None:
    label = name.strip()[:80]
    if not label:
        return None
    cursor = mongodb.db.trigger_rules.find({"owner_id": owner_id, "name": label})
    for rule_doc in await cursor.to_list(20):
        if is_scheduler_managed(rule_doc):
            return rule_doc
    return None


def _replace_existing_series_error(rule_doc: dict) -> CapabilityErrorDetail:
    series_id = str(rule_doc.get("id") or "")
    name = str(rule_doc.get("name") or "that series")
    return _fail(
        f"{name} already exists (series_id={series_id}). "
        f"Use scheduler.replace_alert(series_id={series_id!r}) to change it. "
        "Do not delete and recreate."
    )


async def _require_scheduler_rule(
    owner_id: str, series_id: str
) -> dict | CapabilityErrorDetail:
    rule_doc = await mongodb.db.trigger_rules.find_one(
        {"id": series_id, "owner_id": owner_id},
    )
    if not rule_doc:
        return _fail("Series not found.")
    if not is_scheduler_managed(rule_doc):
        return _scheduler_manage_error(f"series {series_id!r}")
    return rule_doc


async def _scheduler_rule_ids(owner_id: str, rule_ids: set[str]) -> set[str]:
    if not rule_ids:
        return set()
    cursor = mongodb.db.trigger_rules.find(
        {"owner_id": owner_id, "id": {"$in": list(rule_ids)}},
    )
    managed: set[str] = set()
    for rule_doc in await cursor.to_list(None):
        if is_scheduler_managed(rule_doc):
            managed.add(str(rule_doc["id"]))
    return managed


async def _apply_delivery_target(
    kwargs: dict,
    deliver_to: str,
    *,
    owner_id: str,
) -> tuple[dict, CapabilityErrorDetail | None]:
    """Snapshot a delivery target onto the plan from author-time intent.

    ``"anywhere"`` — follow-me (no hint). ``"here"`` — pin origin ``node_id``.
    Any other string — bound room name → ``location_ref``.
    """
    target = (deliver_to or "anywhere").strip()
    if not target or target.casefold() == "anywhere":
        return kwargs, None

    if target.casefold() == "here":
        node_id = get_ctx().get("node_id")
        if not node_id:
            return kwargs, _fail(
                'deliver_to="here" requires a live originating node. '
                'Use deliver_to="anywhere" or a bound room name.'
            )
        delivery = kwargs["delivery"].model_copy(
            update={"target": DeliveryTargetHint(node_id=node_id)},
        )
        return {**kwargs, "delivery": delivery}, None

    location_ref = await resolve_location_ref_for_area_name(owner_id, target)
    if not location_ref:
        bound = await list_bound_room_names(owner_id)
        if bound:
            rooms = ", ".join(bound)
            return kwargs, _fail(
                f"No bound room matched {target!r}. Bound rooms: {rooms}. "
                'Use deliver_to="anywhere", bind the room first, or pick a bound room name.'
            )
        return kwargs, _fail(
            f"No bound room matched {target!r}. No rooms are bound yet. "
            'Use deliver_to="anywhere" or bind a node with jarvis.smart_home.bind_node_area first.'
        )

    delivery = kwargs["delivery"].model_copy(
        update={"target": DeliveryTargetHint(location_ref=location_ref)},
    )
    return {**kwargs, "delivery": delivery}, None


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python", exclude_none=True)
    return value if isinstance(value, dict) else {}


def _delivery_intent(delivery_doc: Any) -> dict[str, Any]:
    delivery = _model_dump(delivery_doc)
    target = _model_dump(delivery.get("target"))
    location_ref = _model_dump(target.get("location_ref"))
    node_id = target.get("node_id")

    if location_ref:
        label = (
            location_ref.get("room_name")
            or location_ref.get("room_id")
            or location_ref.get("ha_area_id")
            or "bound room"
        )
        return {
            "delivery_target": str(label),
            "delivery_target_kind": "room",
        }
    if node_id:
        return {
            "delivery_target": "here",
            "delivery_target_kind": "node",
        }
    return {
        "delivery_target": "anywhere",
        "delivery_target_kind": "follow_me",
    }


async def _apply_delivery_edit(
    delivery: DeliveryPlan,
    deliver_to: str | None,
    *,
    owner_id: str,
) -> tuple[DeliveryPlan, CapabilityErrorDetail | None]:
    if deliver_to is None:
        return delivery, None
    kwargs, error = await _apply_delivery_target(
        {"delivery": delivery},
        deliver_to,
        owner_id=owner_id,
    )
    return kwargs["delivery"], error


def _action_with_updates(
    action_doc: dict[str, Any],
    *,
    message: str | None,
    protocol: str | None,
    instructions: str | None,
) -> TriggerAction:
    action = TriggerAction.model_validate(action_doc)
    updates: dict[str, Any] = {}
    if message is not None:
        updates["message"] = message
    if protocol is not None:
        updates["protocol_name"] = protocol or None
    if instructions is not None:
        updates["instructions"] = instructions or None

    next_action = action.model_copy(update=updates)
    if next_action.protocol_name:
        return next_action.model_copy(update={"decision": DECISION_ACT})
    if instructions is not None and not instructions and next_action.decision == DECISION_OFFER:
        return next_action.model_copy(update={"decision": DECISION_TELL})
    if (
        protocol is not None
        and not protocol
        and next_action.decision == DECISION_ACT
        and not next_action.instructions
    ):
        return next_action.model_copy(update={"decision": DECISION_TELL})
    return next_action


def _validate_importance_edit(
    attention_doc: dict[str, Any],
    importance: Literal["normal", "urgent", "critical"] | None,
) -> CapabilityErrorDetail | None:
    if importance != "critical":
        return None
    attention = AttentionPolicy.model_validate(attention_doc)
    if not attention.requires_ack:
        return _fail(
            "importance='critical' is reserved for alarms. "
            "Use jarvis.scheduler.add_alarm for wake alarms."
        )
    return None


def _attention_with_importance(
    attention_doc: dict[str, Any],
    importance: Literal["normal", "urgent", "critical"] | None,
) -> AttentionPolicy:
    attention = AttentionPolicy.model_validate(attention_doc)
    if importance is None:
        return attention
    if importance == "critical":
        return attention.model_copy(
            update={"level": "critical", "sound": "alarm", "requires_ack": True}
        )
    if attention.requires_ack:
        return attention.model_copy(
            update={"level": importance, "sound": "alarm", "requires_ack": True}
        )
    return attention.model_copy(
        update={
            "level": importance,
            "sound": SOUND_BY_LEVEL.get(importance, attention.sound),
            "requires_ack": False,
        }
    )


def _origin_with_updates(
    origin_doc: dict[str, Any],
    *,
    when: str | None,
    recurrence: str | None,
) -> tuple[TriggerOrigin, datetime | None, CapabilityErrorDetail | None]:
    origin = TriggerOrigin.model_validate(origin_doc)
    next_recurrence = (
        recurrence.lower().strip()
        if recurrence is not None
        else origin.recurrence
    )
    if next_recurrence and not is_valid(next_recurrence):
        return origin, None, _fail(
            f"Invalid recurrence '{next_recurrence}'. "
            "Use: daily, weekdays, weekends, weekly, every Xh, every Xm."
        )

    parsed_when: datetime | None = None
    if when is not None:
        try:
            parsed_when = parse_schedule_time(when, timezone_name=get_timezone())
        except ValueError as exc:
            return origin, None, _fail(
                f"Could not parse when={when!r}. {exc}. "
                "Use formats like '30m', '2h', '17:00', '5pm', "
                "'today 17:00', 'Friday at 5pm', or 'May 12 at 17:00'."
            )

    update: dict[str, Any] = {
        "recurrence": next_recurrence,
    }
    if parsed_when is not None:
        update["fire_at"] = parsed_when
        update["timezone"] = get_timezone()
        if next_recurrence:
            update["original_local_time"] = parsed_when.astimezone(
                coerce_timezone(update["timezone"])
            ).strftime("%H:%M")
        elif recurrence is not None:
            update["original_local_time"] = None
    elif recurrence is not None and not next_recurrence:
        update["original_local_time"] = None

    return origin.model_copy(update=update), parsed_when, None


async def _schedule_next_occurrence(
    *,
    rule_doc: dict[str, Any],
    owner_id: str,
    rule_id: str,
    after_due: datetime | None = None,
) -> datetime | None:
    """Materialize the next pending instance after removing/skipping one occurrence."""
    recurrence_rule = recurrence_rule_from_origin(
        rule_doc["origin"],
        rule_doc=rule_doc,
        owner_id=owner_id,
        rule_id=rule_id,
    )
    recurrence = rule_doc["origin"].get("recurrence", "")
    now = datetime.now(timezone.utc)
    anchor = after_due or now
    if recurrence.startswith("every"):
        next_time = next_occurrence(recurrence_rule, anchor)
    else:
        t = next_occurrence(recurrence_rule, now)
        next_time = next_occurrence(recurrence_rule, t + timedelta(seconds=1)) if t else t
    if not next_time:
        return None

    created = await trigger_service.materialize_recurring_occurrence(
        owner_id=owner_id,
        rule_id=rule_id,
        origin=TriggerOrigin.model_validate(rule_doc["origin"]),
        action=TriggerAction.model_validate(rule_doc["action"]),
        attention=AttentionPolicy.model_validate(rule_doc["attention"]),
        delivery=DeliveryPlan.model_validate(rule_doc["delivery"]),
        freshness=FreshnessPolicy.model_validate(rule_doc["freshness"]),
        due_at=next_time,
        management=ManagementOwnership.model_validate(rule_doc["management"]),
    )
    return next_time if created else None


class SchedulerPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="scheduler",
        version="3.0.0",
        description="Create, find, and manage time-based scheduled work: reminders, timers, alarms, and deferred/silent instructions.",
        utterances=[
            "remind me later to do something",
            "remind me tomorrow to do something",
            "remind me tonight to handle a task",
            "remind me in a few minutes",
            "turn the lights off in 10 minutes",
            "in 5 minutes do something for me",
            "check on something later and only tell me if needed",
            "set a reminder at a named time",
            "set a recurring reminder",
            "start a timer",
            "set an alarm",
            "wake me up at a given time",
            "brief me at a scheduled time",
            "cancel, snooze, or list my reminders and alarms",
            "cancel that thing I asked you to do later",
            "cancel the soccer lights automation I set for 8pm",
            "don't turn the lights off later after all",
            "what reminders and timers do I have",
            "show my upcoming alerts and reminders today",
            "what is due to remind me today",
            "which alarms and reminders are coming up",
            "change the morning bedroom lights time from now on",
        ],
    )

    # ------------------------------------------------------------------
    # Primary tool
    # ------------------------------------------------------------------

    @tool
    async def remind(
        self,
        when: str,
        message: str,
        recurrence: str | None = None,
        importance: Literal["normal", "urgent"] = "normal",
        protocol: str | None = None,
        instructions: str | None = None,
        decision: Literal["tell", "offer"] = "tell",
        deliver_to: str = "anywhere",
        confirmed: bool = False,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Create a new message or task reminder. Does not change an existing reminder.
        Duration and wall-clock times both set a due time; they do not change this into a timer.
        Use add_timer only when the user asks for a countdown timer, and add_alarm for new wake alarms.
        Use defer for silent "do this later" side effects; reminders tell the user.
        Do NOT use automations.create_rule for time-based requests.
        To change an existing reminder, use replace_alert; do not create a second one and cancel.
        message is static text to repeat at fire time. For live future work, set a short message plus instructions with what to do at fire time.
        protocol runs an existing named protocol; call setups.find(setup_type="protocol") first if unsure it exists.
        Do not use protocol to attach a reminder to a habit, task, device, or other domain object; use that domain plugin, then cancel the old generic reminder if needed.
        Example live briefing: remind(when="tomorrow 10:00", message="Briefing", instructions="Gather the relevant live data, then speak a concise briefing.")

        Args:
            when: Duration or wall-clock time: "in 2 minutes", "30m", "17:00", "5pm",
                "today 17:00", "tomorrow 9am", "Friday at 5pm",
                "May 12 at 17:00", or ISO datetime.
            message: What to say or display when it fires.
            recurrence: "daily", "weekdays", "weekends", "weekly", "every Xh", "every Xm".
            importance: how hard it should reach the user when it fires.
                "normal" — announced when the user is active; held quietly during quiet hours / Do Not Disturb.
                "urgent" — breaks through quiet hours ("don't let me miss this"); timer sound.
                Pick from intent, not loudness. Most reminders are "normal".
            protocol: Run this named protocol at fire time instead of speaking message directly.
            instructions: Fire-time work or delivery criteria. For decision="offer",
                state the underlying decision clearly: when to speak now, when to
                defer, and when to suppress because it is already handled or no
                longer useful.
            decision: "tell" speaks the message; "offer" evaluates instructions and live state before speaking.
            deliver_to: Where the reminder should fire.
                "anywhere" (default) — follow-me on the last-active speaker.
                "here" — pin to this node at creation (physical task in this room).
                A bound room name (e.g. "bedroom") — ring wherever that room's speaker is live.
            confirmed: For recurring reminders, false returns a preview without creating the rule; call again with true to persist.
        """
        if recurrence and not is_valid(recurrence):
            return _fail(f"Invalid recurrence '{recurrence}'. Use: daily, weekdays, weekends, weekly, every Xh, every Xm.")

        owner_id = get_owner_id()
        if protocol:
            from plugins.protocol import protocol_exists
            if not await protocol_exists(protocol, owner_id):
                return _fail(
                    f"Protocol '{protocol}' not found. Use setups.find(setup_type='protocol') "
                    "to choose an existing protocol, or use instructions=... for a one-off live briefing."
                )

        if decision == "offer" and not instructions:
            return _fail('decision="offer" requires instructions.')

        tz_name = get_timezone()
        tz = coerce_timezone(tz_name)
        try:
            trigger_time = _parse_future_when(when, timezone_name=tz_name)
        except ValueError as exc:
            return _fail(
                f"Could not parse when={when!r}. {exc}. "
                'Use formats like "in 2 minutes", "30m", "17:00", "5pm", '
                "'today 17:00', 'Friday at 5pm', or 'May 12 at 17:00'."
            )

        recurrence = recurrence.lower().strip() if recurrence else None
        original_local_time = (
            trigger_time.astimezone(tz).strftime("%H:%M")
            if recurrence else None
        )
        kwargs, deliver_err = await _apply_delivery_target(
            reminder_preset(
                owner_id=owner_id,
                message=message,
                fire_at=trigger_time,
                recurrence=recurrence,
                timezone_name=tz_name,
                original_local_time=original_local_time,
                protocol_name=protocol,
                instructions=instructions,
                decision=decision,
                importance=importance,
            ),
            deliver_to,
            owner_id=owner_id,
        )
        if deliver_err:
            return deliver_err

        rule_id: str | None = None
        if recurrence:
            existing = await _existing_named_series(owner_id, message)
            if existing:
                return _replace_existing_series_error(existing)
            if not confirmed:
                sections = [
                    {
                        "type": "kv",
                        "pairs": {
                            "Message": message,
                            "Starts": format_local_when(trigger_time),
                            "Recurrence": recurrence,
                            "Importance": importance,
                            "Protocol": protocol or "None",
                            "Decision": kwargs["action"].decision,
                            "Instructions": instructions or "None",
                        },
                    },
                    {
                        "type": "markdown",
                        "content": (
                            "Preview only. No recurring reminder was created.\n\n"
                            "If this is correct, call `jarvis.scheduler.remind(..., confirmed=True)` "
                            "with the same fields."
                        ),
                    },
                ]
                return ToolResult(
                    content=(
                        f"Preview only. Recurring reminder '{message}' was not created. "
                        "Confirm before persisting this recurring behavior."
                    ),
                    ui=[content_envelope("Recurring Reminder Preview", sections)],
                )

            rule = await trigger_service.create_rule(
                owner_id=owner_id,
                name=message[:80],
                origin=TriggerOrigin(
                    kind="time",
                    fire_at=trigger_time,
                    recurrence=recurrence,
                    timezone=tz_name,
                    original_local_time=original_local_time,
                ),
                action=kwargs["action"],
                attention=kwargs["attention"],
                delivery=kwargs["delivery"],
                freshness=kwargs["freshness"],
                management=ManagementOwnership(provider="scheduler"),
            )
            rule_id = rule.id
            kwargs["management"] = rule.management

        instance = await trigger_service.create_instance(
            rule_id=rule_id,
            due_at=trigger_time,
            **kwargs,
        )

        label = "Reminder"
        when_clause = format_local_when(trigger_time)
        if recurrence:
            content = (
                f"Recurring {label.lower()} ({recurrence}) set. "
                f"Series ID: {rule_id}. Starting {when_clause}."
            )
            control_hint = (
                f"Cancel with series_id {rule_id}."
                if rule_id else "Use scheduler tools to pause or cancel this recurring reminder."
            )
            await publish_operations_changed(owner_id, "schedules")
            return ToolResult(
                content=content,
                ui=[receipt_envelope(
                    "Recurring Reminder",
                    f"{message} · {recurrence}",
                    sublabel=f"Starts {_format_receipt_when(trigger_time)}. {control_hint}",
                )],
            )
        content = f"{label} set for {when_clause}. ID: {instance.id}"
        return ToolResult(
            content=content,
            ui=[receipt_envelope(label, message, sublabel=_format_receipt_when(trigger_time))],
        )

    @tool
    async def defer(
        self,
        when: str,
        instruction: str,
        recurrence: str | None = None,
        decision: TriggerDecision = DECISION_ACT,
        deliver_to: str = "anywhere",
        confirmed: bool = False,
    ) -> str | ToolResult | CapabilityErrorDetail:
        """
        Schedule a silent instruction to do something later, not a reminder or timer.
        Use for "turn off the lights in 10 minutes", recurring light routines, "check the garage later and tell me if it is open", or any time-based side-effect work.
        Use remind/add_timer/add_alarm only when the user wants to be told something.
        Cancel with get_alerts then cancel_alert(instance_id=...), or pass the returned ID directly.

        Args:
            when: "30s", "5m", "1h", "17:00", "5pm", "tomorrow 9am", or ISO datetime.
            instruction: The task to execute at fire time with jarvis.* tools.
            recurrence: "daily", "weekdays", "weekends", "weekly", "every Xh", "every Xm".
            decision: "act" (default) runs silently; "offer" evaluates before any follow-up speech.
            deliver_to: "anywhere" (default), "here", or a bound room name for any needed follow-up speech.
            confirmed: For recurring deferred instructions, false returns a preview; call again with true to persist.
        """
        if recurrence and not is_valid(recurrence):
            return _fail(f"Invalid recurrence '{recurrence}'. Use: daily, weekdays, weekends, weekly, every Xh, every Xm.")

        owner_id = get_owner_id()
        tz_name = get_timezone()
        tz = coerce_timezone(tz_name)
        try:
            trigger_time = _parse_future_when(when, timezone_name=tz_name)
        except ValueError as exc:
            return _fail(
                f"Could not parse when={when!r}. {exc}. "
                "Use formats like '30s', '5m', '1h', '17:00', '5pm', "
                "'tomorrow 9am', or ISO datetime."
            )

        recurrence = recurrence.lower().strip() if recurrence else None
        original_local_time = (
            trigger_time.astimezone(tz).strftime("%H:%M")
            if recurrence else None
        )
        kwargs, deliver_err = await _apply_delivery_target(
            deferred_instruction_preset(
                owner_id=owner_id,
                instruction=instruction,
                fire_at=trigger_time,
                decision=decision,
            ),
            deliver_to,
            owner_id=owner_id,
        )
        if deliver_err:
            return deliver_err

        if recurrence:
            existing = await _existing_named_series(owner_id, instruction)
            if existing:
                return _replace_existing_series_error(existing)
            kwargs["origin"] = TriggerOrigin(
                kind="time",
                fire_at=trigger_time,
                recurrence=recurrence,
                timezone=tz_name,
                original_local_time=original_local_time,
            )
            if not confirmed:
                sections = [
                    {
                        "type": "kv",
                        "pairs": {
                            "Instruction": instruction,
                            "Starts": format_local_when(trigger_time),
                            "Recurrence": recurrence,
                            "Decision": kwargs["action"].decision,
                        },
                    },
                    {
                        "type": "markdown",
                        "content": (
                            "Preview only. No recurring deferred instruction was created.\n\n"
                            "If this is correct, call `jarvis.scheduler.defer(..., confirmed=True)` "
                            "with the same fields."
                        ),
                    },
                ]
                return ToolResult(
                    content=(
                        f"Preview only. Recurring deferred instruction '{instruction[:80]}' "
                        "was not created. Confirm before persisting this recurring behavior."
                    ),
                    ui=[content_envelope("Recurring Deferred Instruction Preview", sections)],
                )

            rule = await trigger_service.create_rule(
                owner_id=owner_id,
                name=instruction[:80],
                origin=kwargs["origin"],
                action=kwargs["action"],
                attention=kwargs["attention"],
                delivery=kwargs["delivery"],
                freshness=kwargs["freshness"],
                management=ManagementOwnership(provider="scheduler"),
            )
            instance = await trigger_service.create_instance(
                rule_id=rule.id,
                due_at=trigger_time,
                management=rule.management,
                **kwargs,
            )
            await publish_operations_changed(owner_id, "schedules")
            when_clause = format_local_when(trigger_time)
            return (
                f"Recurring deferred instruction ({recurrence}) scheduled. "
                f"Series ID: {rule.id}. Starting {when_clause}. First instance ID: {instance.id}"
            )

        instance = await trigger_service.create_instance(
            due_at=trigger_time,
            **kwargs,
        )
        when_clause = format_local_when(trigger_time)
        return f"Deferred instruction scheduled for {when_clause}. ID: {instance.id}"

    @tool
    async def add_timer(
        self, duration: str, message: str = "Time's up!",
        protocol: str | None = None,
        deliver_to: str = "here",
    ) -> str | CapabilityErrorDetail:
        """
        Start a countdown timer. One-shot interval; stops without snooze.
        Use for relative durations ("20 minutes") or countdown-to-clock ("5pm", "5 o'clock").
        For "do X after N minutes" side-effect work, use defer instead.
        To clear a running timer, use cancel_alert with its instance_id.
        Args:
            duration: "30s", "5m", "5m 30s", "1h", "90m", or a wall-clock time to count down to.
            deliver_to: "here" (default), "anywhere", or a bound room name.
        """
        owner_id = get_owner_id()
        tz_name = get_timezone()
        now_utc = datetime.now(timezone.utc)
        try:
            trigger_time = _parse_future_when(
                duration, now=now_utc, timezone_name=tz_name
            )
        except ValueError as exc:
            return _fail(f"Could not parse duration={duration!r}. {exc}. Use formats like '30s', '5m', '5m 30s', '1h', or '5pm'.")
        duration_s = max(0, int((trigger_time - now_utc).total_seconds()))
        kwargs, deliver_err = await _apply_delivery_target(
            timer_preset(owner_id=owner_id, message=message, duration_s=duration_s),
            deliver_to,
            owner_id=owner_id,
        )
        if deliver_err:
            return deliver_err
        if protocol:
            kwargs["action"] = TriggerAction(
                decision=DECISION_ACT,
                message=message,
                protocol_name=protocol,
            )
        instance = await trigger_service.create_instance(**kwargs)
        when_clause = format_local_when(trigger_time, now=now_utc)
        return f"Timer set for {when_clause}. ID: {instance.id}"

    @tool
    async def add_alarm(
        self, time_str: str, message: str = "Alarm!",
        recurrence: str | None = None, protocol: str | None = None,
        deliver_to: str = "anywhere",
    ) -> str | CapabilityErrorDetail:
        """
        Create a new snoozable alarm; use replace_alert when the user refers to changing an existing one.
        Critical attention, alarm sound, explicit acknowledgement. remind is a notification, not a snoozable alarm.
        Use replace_alert to change an existing alarm's time, room, or message. Use snooze_alert to extend a fired alarm.
        Accepts wall-clock times and relative durations such as "30m" or "in 2 hours".
        Args:
            time_str: "30m", "in 2 hours", "17:00", "5pm", "5 o'clock",
                "tomorrow 7am", "Friday at 7am", or ISO datetime.
            recurrence: "daily", "weekdays", "weekends", "weekly", "every Xh", "every Xm".
            deliver_to: "anywhere" (default), "here", or a bound room name (e.g. "bedroom" for wake alarms).
        """
        if recurrence and not is_valid(recurrence):
            return _fail(f"Invalid recurrence '{recurrence}'.")

        owner_id = get_owner_id()
        tz_name = get_timezone()
        tz = coerce_timezone(tz_name)
        try:
            trigger_time = _parse_future_when(time_str, timezone_name=tz_name)
        except ValueError as exc:
            return _fail(
                f"Could not parse time_str={time_str!r}. {exc}. "
                f'Use formats like "30m", "17:00", "5pm", "5 o\'clock", "today 17:00", or "Friday at 7am".'
            )

        kwargs, deliver_err = await _apply_delivery_target(
            alarm_preset(
                owner_id=owner_id, message=message, fire_at=trigger_time,
                recurrence=recurrence, timezone_name=tz_name,
                original_local_time=(
                    trigger_time.astimezone(tz).strftime("%H:%M")
                    if recurrence else None
                ),
            ),
            deliver_to,
            owner_id=owner_id,
        )
        if deliver_err:
            return deliver_err
        kwargs["delivery"] = with_target_fallback_for_critical(
            kwargs["delivery"],
            kwargs["attention"],
        )

        rule_id: str | None = None
        if recurrence:
            recurrence = recurrence.lower().strip()
            original_local_time = trigger_time.astimezone(tz).strftime("%H:%M")
            rule = await trigger_service.create_rule(
                owner_id=owner_id,
                name=message[:80],
                origin=TriggerOrigin(
                    kind="time", fire_at=trigger_time, recurrence=recurrence,
                    timezone=tz_name, original_local_time=original_local_time,
                ),
                action=kwargs["action"],
                attention=kwargs["attention"],
                delivery=kwargs["delivery"],
                freshness=kwargs["freshness"],
                management=ManagementOwnership(provider="scheduler"),
            )
            rule_id = rule.id
            kwargs["rule_id"] = rule_id
            kwargs["management"] = rule.management

        instance = await trigger_service.create_instance(**kwargs)
        when_clause = format_local_when(trigger_time)
        if recurrence:
            await publish_operations_changed(owner_id, "schedules")
            return f"Recurring alarm ({recurrence}) set. Series ID: {rule_id}. Starting {when_clause}."
        return f"Alarm set for {when_clause}. ID: {instance.id}"

    # ------------------------------------------------------------------
    # List / cancel / snooze
    # ------------------------------------------------------------------

    @tool
    async def get_alerts(
        self,
        kind: Literal["timer", "alarm", "reminder"] | None = None,
        status: Literal["pending", "awaiting_delivery", "active"] | None = None,
        recurrence: str | None = None,
        local_time: str | None = None,
        message: str | None = None,
        query: str | None = None,
    ) -> AlertQueryResult:
        """
        Find pending time-based work: reminders, timers, alarms, and silent defer() instructions.
        Omit status to list upcoming pending occurrences; status="active" is a recurring series with no pending occurrence.
        Use this for upcoming one-offs and recurring schedules before cancel/snooze/skip/replace;
        returns instance_id and series_id. If the user calls a timed action an "automation",
        "schedule", or "rule", still search here with query=/message= — not setups.find.
        Filter named reminders/timers/alarms with kind; search other scheduled work by query or message
        (both match message+instructions, or a clock time such as 12:00 / 12pm). For durable
        external-event rules and protocol definitions, use setups.find.
        """
        owner_id = get_owner_id()
        now_utc = datetime.now(timezone.utc)
        text_filter = (query or message or "").strip() or None
        normalized_local_time = normalize_clock_time(local_time)
        if normalized_local_time is None:
            clock_from_query = normalize_clock_time(text_filter)
            if clock_from_query:
                normalized_local_time = clock_from_query
                text_filter = None
        instance_statuses = ["pending"] if status is None else [status]
        docs = []
        if status != "active":
            cursor = mongodb.db.trigger_instances.find(
                {
                    "owner_id": owner_id,
                    "status": {"$in": instance_statuses},
                }
            )
            docs = await cursor.to_list(None)

        linked_rule_ids = {doc.get("rule_id") for doc in docs if doc.get("rule_id")}
        managed_rule_ids = await _scheduler_rule_ids(owner_id, {str(rid) for rid in linked_rule_ids if rid})
        rule_names: dict[str, str] = {}
        if managed_rule_ids:
            cursor = mongodb.db.trigger_rules.find(
                {"owner_id": owner_id, "id": {"$in": list(managed_rule_ids)}},
                {"_id": 0, "id": 1, "name": 1},
            )
            rule_names = {
                str(rule["id"]): str(rule.get("name") or "")
                for rule in await cursor.to_list(None)
                if rule.get("id")
            }

        results = []
        for doc in docs:
            rule_id = doc.get("rule_id")
            if rule_id and str(rule_id) not in managed_rule_ids:
                continue
            if not rule_id and not is_scheduler_managed_instance(doc):
                continue
            due_at = coerce_datetime(doc.get("due_at"), default=now_utc)
            results.append(
                _alert_list_entry(
                    instance_id=doc["id"],
                    series_id=doc.get("rule_id"),
                    scope="instance",
                    name=rule_names.get(str(doc.get("rule_id") or ""), ""),
                    due_at=due_at,
                    action=doc.get("action_snapshot", {}),
                    attention=doc.get("attention_snapshot", {}),
                    trigger=doc.get("origin_snapshot", {}),
                    status=doc["status"],
                    delivery_snapshot=doc.get("delivery_snapshot", {}),
                    now_utc=now_utc,
                )
            )

        results.extend(await self._recurring_series_without_upcoming(owner_id, results))
        if kind or status or recurrence or normalized_local_time or text_filter:
            results = [
                row for row in results
                if _alert_matches_filters(
                    row,
                    kind=kind,
                    status=status,
                    recurrence=recurrence,
                    local_time=normalized_local_time,
                    message=text_filter,
                )
            ]

        sorted_results = sorted(
            results,
            key=lambda item: (item.get("utc_time") is None, item.get("utc_time") or ""),
        )
        alerts = [AlertSummary.model_validate(row) for row in sorted_results]
        return AlertQueryResult(
            alerts=alerts,
            match_status=match_status_from_count(len(alerts)),
            coverage=ReadCoverage.COMPLETE,
            kind=kind,
            query=query,
        )

    @tool
    async def get_next_alert(
        self,
        kind: Literal["timer", "alarm", "reminder"] | None = None,
        recurrence: str | None = None,
        local_time: str | None = None,
        message: str | None = None,
        query: str | None = None,
    ) -> AlertSummary | CapabilityErrorDetail:
        """
        Get the next pending reminder, timer, or alarm. Silent instructions are not alerts.
        """
        result = await self.get_alerts(
            kind=kind,
            status="pending",
            recurrence=recurrence,
            local_time=local_time,
            message=message,
            query=query,
        )
        for row in result.alerts:
            if row.kind is None:
                continue
            if row.scope == "instance" and row.time:
                return row
        noun = f" {kind}" if kind else ""
        return _fail(f"No pending{noun} alert found.")

    @staticmethod
    async def _recurring_series_without_upcoming(
        owner_id: str, instance_entries: List[Dict]
    ) -> List[Dict]:
        """Recurring rules that have no pending instance — otherwise invisible to listing."""
        seen_series_ids = {entry["series_id"] for entry in instance_entries if entry.get("series_id")}
        cursor = mongodb.db.trigger_rules.find(
            {
                "owner_id": owner_id,
                "enabled": True,
                "origin.kind": {"$in": ["time", "interval"]},
                "origin.recurrence": {"$nin": [None, ""]},
                "surface": True,
            }
        )
        entries: List[Dict] = []
        for rule in await cursor.to_list(None):
            if not is_scheduler_managed(rule):
                continue
            if rule.get("id") in seen_series_ids:
                continue
            origin = rule.get("origin", {})
            action = rule.get("action", {})
            attention = rule.get("attention", {})
            rule_id = rule["id"]
            entries.append(
                _alert_list_entry(
                    instance_id=None,
                    series_id=rule_id,
                    scope="series",
                    name=str(rule.get("name") or ""),
                    due_at=None,
                    action=action,
                    attention=attention,
                    trigger=origin,
                    status="active",
                    delivery_snapshot=rule.get("delivery", {}),
                    now_utc=datetime.now(timezone.utc),
                )
            )
        return entries

    @tool
    async def replace_alert(
        self,
        series_id: str | None = None,
        instance_id: str | None = None,
        query: str | None = None,
        kind: Literal["timer", "alarm", "reminder"] | None = None,
        scope: Literal["occurrence", "series"] = "occurrence",
        when: str | None = None,
        message: str | None = None,
        recurrence: str | None = None,
        importance: Literal["normal", "urgent", "critical"] | None = None,
        protocol: str | None = None,
        instructions: str | None = None,
        deliver_to: str | None = None,
    ) -> str | CapabilityErrorDetail:
        """
        Change an existing alarm, reminder, or timer. query= names it; scope=series means all future occurrences.
        Use series_id for permanent series changes ("from now on"). Use instance_id for one pending occurrence ("today", "tomorrow", "just this once").
        Do not pass recurrence when only changing the time. Do not cancel and recreate. To clear/stop, use cancel_alert.
        deliver_to: "anywhere", "here", or a bound room name.

        Args:
            query: Current name or clock time of the existing item, such as "wake" or "8:30". Not the new time.
            scope: "occurrence" (default) changes one pending item; "series" changes all future occurrences.
            series_id: series_id from get_alerts(). Changes the durable recurring schedule.
            instance_id: instance_id from get_alerts(). Changes only that pending occurrence.
        """
        owner_id = get_owner_id()
        if series_id and instance_id:
            return _one_scope_required_error("replace_alert")
        if (series_id or instance_id) and query:
            return _fail("Pass query= or an id, not both.")

        if not series_id and not instance_id:
            if not query and not kind and when is None and message is None and deliver_to is None:
                return _one_scope_required_error("replace_alert")
            resolved = await self._resolve_alert_target(query=query, kind=kind)
            if isinstance(resolved, CapabilityErrorDetail):
                return resolved
            series_id, instance_id = _ids_from_resolved(resolved, scope=scope)
            if not series_id and not instance_id:
                return _fail(f"Matched item has no {scope} identifier.")

        if protocol:
            from plugins.protocol import protocol_exists
            if not await protocol_exists(protocol, owner_id):
                return _fail(
                    f"Protocol '{protocol}' not found. Use setups.find(setup_type='protocol') "
                    "to choose an existing protocol, or use instructions=... for a one-off live briefing."
                )

        if series_id:
            return await self._replace_series(
                series_id=series_id,
                when=when,
                message=message,
                recurrence=recurrence,
                importance=importance,
                protocol=protocol,
                instructions=instructions,
                deliver_to=deliver_to,
                owner_id=owner_id,
            )

        return await self._replace_instance(
            instance_id=instance_id or "",
            when=when,
            message=message,
            recurrence=recurrence,
            importance=importance,
            protocol=protocol,
            instructions=instructions,
            deliver_to=deliver_to,
            owner_id=owner_id,
        )

    async def _resolve_alert_target(
        self,
        *,
        query: str | None,
        kind: Literal["timer", "alarm", "reminder"] | None = None,
    ) -> AlertSummary | CapabilityErrorDetail:
        result = await self.get_alerts(kind=kind, query=query)
        if result.match_status == MatchStatus.NONE:
            return _fail(
                "No matching pending alarm, reminder, or timer. "
                "Use add_alarm, remind, or add_timer to create one."
            )
        if result.match_status == MatchStatus.MULTIPLE:
            return _ambiguous_alerts_error(result.alerts)
        return result.alerts[0]

    async def _replace_series(
        self,
        *,
        series_id: str,
        when: str | None,
        message: str | None,
        recurrence: str | None,
        importance: Literal["normal", "urgent", "critical"] | None,
        protocol: str | None,
        instructions: str | None,
        deliver_to: str | None,
        owner_id: str,
    ) -> str | CapabilityErrorDetail:
        rule_doc = await _require_scheduler_rule(owner_id, series_id)
        if isinstance(rule_doc, CapabilityErrorDetail):
            return rule_doc

        origin, next_due, origin_error = _origin_with_updates(
            rule_doc.get("origin", {}),
            when=when,
            recurrence=recurrence,
        )
        if origin_error:
            return origin_error

        action = _action_with_updates(
            rule_doc.get("action", {"decision": DECISION_TELL}),
            message=message,
            protocol=protocol,
            instructions=instructions,
        )
        importance_error = _validate_importance_edit(rule_doc.get("attention", {}), importance)
        if importance_error:
            return importance_error
        attention = _attention_with_importance(rule_doc.get("attention", {}), importance)
        delivery, delivery_error = await _apply_delivery_edit(
            DeliveryPlan.model_validate(rule_doc.get("delivery", {})),
            deliver_to,
            owner_id=owner_id,
        )
        if delivery_error:
            return delivery_error
        delivery = with_target_fallback_for_critical(delivery, attention)

        now = datetime.now(timezone.utc)
        previous_recurrence = str(
            (rule_doc.get("origin") or {}).get("recurrence") or ""
        ).lower().strip()
        next_recurrence = str(origin.recurrence or "").lower().strip()
        recurrence_changed = (
            recurrence is not None and next_recurrence != previous_recurrence
        )
        rule_update = {
            "origin": origin.model_dump(mode="python", exclude_none=True),
            "action": action.model_dump(mode="python", exclude_none=True),
            "attention": attention.model_dump(mode="python", exclude_none=True),
            "delivery": delivery.model_dump(mode="python", exclude_none=True),
            "updated_at": now,
        }
        await mongodb.db.trigger_rules.update_one(
            {"id": series_id, "owner_id": owner_id},
            {"$set": rule_update},
        )

        instance_set: dict[str, Any] = {
            "origin_snapshot": rule_update["origin"],
            "action_snapshot": rule_update["action"],
            "attention_snapshot": rule_update["attention"],
            "delivery_snapshot": rule_update["delivery"],
            "updated_at": now,
        }
        if next_due is not None:
            instance_set["due_at"] = next_due
        elif recurrence_changed:
            computed = next_occurrence(
                recurrence_rule_from_origin(
                    origin.model_dump(mode="python", exclude_none=True),
                    rule_doc=rule_doc,
                    owner_id=owner_id,
                    rule_id=series_id,
                ),
                now,
            )
            if computed is not None:
                instance_set["due_at"] = computed
                next_due = computed

        await mongodb.db.trigger_instances.update_many(
            {
                "rule_id": series_id,
                "owner_id": owner_id,
                "status": "pending",
            },
            {"$set": instance_set},
        )
        await publish_operations_changed(owner_id, "schedules")
        when_clause = format_local_when(next_due) if next_due else "its current schedule"
        return f"Series updated. Series ID: {series_id}. Next occurrence {when_clause}."

    async def _replace_instance(
        self,
        *,
        instance_id: str,
        when: str | None,
        message: str | None,
        recurrence: str | None,
        importance: Literal["normal", "urgent", "critical"] | None,
        protocol: str | None,
        instructions: str | None,
        deliver_to: str | None,
        owner_id: str,
    ) -> str | CapabilityErrorDetail:
        doc = await mongodb.db.trigger_instances.find_one(
            {"id": instance_id, "owner_id": owner_id}
        )
        if not doc:
            return _fail(f"Notification instance not found. Use instance_id from get_alerts(), got {instance_id!r}.")
        if recurrence:
            return _fail("Omit recurrence for one-off occurrence edits; use series_id only for all-future changes.")
        if doc.get("rule_id"):
            rule = await _require_scheduler_rule(owner_id, str(doc["rule_id"]))
            if isinstance(rule, CapabilityErrorDetail):
                return rule
        elif not is_scheduler_managed_instance(doc):
            return _scheduler_manage_error(f"occurrence {instance_id!r}")
        if doc.get("status") not in PENDING_EDIT_STATUSES:
            return _fail("Notification is not pending.")

        origin, due_at, origin_error = _origin_with_updates(
            doc.get("origin_snapshot", {}),
            when=when,
            recurrence=None,
        )
        if origin_error:
            return origin_error
        if due_at is None:
            due_at = coerce_datetime(doc.get("due_at"))

        action = _action_with_updates(
            doc.get("action_snapshot", {"decision": DECISION_TELL}),
            message=message,
            protocol=protocol,
            instructions=instructions,
        )
        importance_error = _validate_importance_edit(doc.get("attention_snapshot", {}), importance)
        if importance_error:
            return importance_error
        attention = _attention_with_importance(doc.get("attention_snapshot", {}), importance)
        delivery, delivery_error = await _apply_delivery_edit(
            DeliveryPlan.model_validate(doc.get("delivery_snapshot", {})),
            deliver_to,
            owner_id=owner_id,
        )
        if delivery_error:
            return delivery_error
        delivery = with_target_fallback_for_critical(delivery, attention)

        update = {
            "due_at": due_at,
            "origin_snapshot": origin.model_dump(mode="python", exclude_none=True),
            "action_snapshot": action.model_dump(mode="python", exclude_none=True),
            "attention_snapshot": attention.model_dump(mode="python", exclude_none=True),
            "delivery_snapshot": delivery.model_dump(mode="python", exclude_none=True),
            "updated_at": datetime.now(timezone.utc),
        }
        await mongodb.db.trigger_instances.update_one(
            {"id": instance_id, "owner_id": owner_id},
            {"$set": update},
        )
        await publish_operations_changed(owner_id, "schedules")
        label = "Occurrence" if doc.get("rule_id") else "Notification"
        return f"{label} updated. Instance ID: {instance_id}. Due {format_local_when(due_at)}."

    @tool
    async def cancel_alert(
        self,
        series_id: str | None = None,
        instance_id: str | None = None,
        query: str | None = None,
        kind: Literal["timer", "alarm", "reminder"] | None = None,
        scope: Literal["occurrence", "series"] = "occurrence",
    ) -> str | CapabilityErrorDetail:
        """
        Cancel an existing alarm, reminder, or timer. query= names it; scope=series cancels all future occurrences.
        Use instance_id for one pending occurrence; series_id for all-future recurring cancel.
        """
        owner_id = get_owner_id()
        now = datetime.now(timezone.utc)
        if series_id and instance_id:
            return _one_scope_required_error("cancel_alert")
        if (series_id or instance_id) and query:
            return _fail("Pass query= or an id, not both.")
        if not series_id and not instance_id:
            if not query and not kind:
                return _one_scope_required_error("cancel_alert")
            resolved = await self._resolve_alert_target(query=query, kind=kind)
            if isinstance(resolved, CapabilityErrorDetail):
                return resolved
            series_id, instance_id = _ids_from_resolved(resolved, scope=scope)
            if not series_id and not instance_id:
                return _fail(f"Matched item has no {scope} identifier.")

        if series_id:
            rule = await _require_scheduler_rule(owner_id, series_id)
            if isinstance(rule, CapabilityErrorDetail):
                return rule
            await mongodb.db.trigger_rules.delete_one(
                {"id": series_id, "owner_id": owner_id},
            )
            await mongodb.db.trigger_instances.update_many(
                {
                    "rule_id": series_id,
                    "owner_id": owner_id,
                    "status": {"$in": ["pending", "awaiting_delivery"]},
                },
                {"$set": {"status": "cancelled", "completed_at": now, "updated_at": now}},
            )
            await publish_operations_changed(owner_id, "schedules")
            return "Recurring series cancelled."

        instance = await mongodb.db.trigger_instances.find_one(
            {"id": instance_id, "owner_id": owner_id}
        )
        if not instance:
            return _fail("Notification not found.")

        rule_id = instance.get("rule_id")
        if rule_id:
            rule_doc = await _require_scheduler_rule(owner_id, str(rule_id))
            if isinstance(rule_doc, CapabilityErrorDetail):
                return rule_doc
            if instance.get("status") not in PENDING_EDIT_STATUSES:
                return _fail("Notification is not pending.")
            skipped_due = coerce_datetime(instance.get("due_at"))
            await trigger_service.cancel_instance(instance_id or "")
            await publish_operations_changed(owner_id, "schedules")
            next_time = await _schedule_next_occurrence(
                rule_doc=rule_doc,
                owner_id=owner_id,
                rule_id=rule_id,
                after_due=skipped_due,
            )
            if next_time:
                return (
                    f"Occurrence cancelled. "
                    f"Next occurrence at {format_local_when(next_time)}."
                )
            return "Occurrence cancelled."

        if not is_scheduler_managed_instance(instance):
            return _scheduler_manage_error(f"occurrence {instance_id!r}")
        await trigger_service.cancel_instance(instance_id or "")
        await publish_operations_changed(owner_id, "schedules")
        return "Notification cancelled."

    @tool
    async def snooze_alert(self, duration: str = "10m", instance_id: str | None = None) -> str | CapabilityErrorDetail:
        """
        Snooze a fired alarm, timer, or reminder. Keeps the original sound, acknowledgement, and delivery target.
        Pass the user's duration as-is instead of converting it to a clock time.

        Args:
            duration: "5m", "10m", "1h", or a natural local time like "tomorrow 9am".
            instance_id: Instance id to snooze. If omitted, snoozes the most recently fired.
        """
        owner_id = get_owner_id()
        tz_name = get_timezone()
        try:
            snooze_until = parse_schedule_time(duration, timezone_name=tz_name)
        except ValueError as exc:
            return _fail(f"Could not parse duration={duration!r}. {exc}.")

        if instance_id:
            doc = await mongodb.db.trigger_instances.find_one(
                {"id": instance_id, "owner_id": owner_id}
            )
        else:
            doc = await trigger_service.get_ackable_for_owner(owner_id)

        if not doc:
            return _fail("No notification to snooze.")
        if doc.get("rule_id"):
            rule = await _require_scheduler_rule(owner_id, str(doc["rule_id"]))
            if isinstance(rule, CapabilityErrorDetail):
                return rule
        elif not is_scheduler_managed_instance(doc):
            return _scheduler_manage_error(f"occurrence {doc['id']!r}")

        new_instance = await trigger_service.snooze_instance(
            doc["id"],
            snooze_until=snooze_until,
        )
        if not new_instance:
            return _fail("Could not snooze.")
        return f"Snoozed. Next reminder at {format_local_when(snooze_until)}"

    # ------------------------------------------------------------------
    # Series control (operate on trigger_rules)
    # ------------------------------------------------------------------

    @tool
    async def skip_next(self, series_id: str) -> str | CapabilityErrorDetail:
        """
        Skip the next occurrence of a recurring notification without changing its time or message.
        The series stays active. Use cancel_alert(instance_id=...) to cancel one occurrence.
        Args:
            series_id: series_id from get_alerts().
        """
        owner_id = get_owner_id()
        rule_doc = await _require_scheduler_rule(owner_id, series_id)
        if isinstance(rule_doc, CapabilityErrorDetail):
            return rule_doc

        skipped = await mongodb.db.trigger_instances.find_one_and_delete(
            {"rule_id": series_id, "status": "pending", "owner_id": owner_id}
        )
        if not skipped:
            return _fail("No pending occurrence to skip.")

        skipped_due = coerce_datetime(skipped.get("due_at"))
        next_time = await _schedule_next_occurrence(
            rule_doc=rule_doc,
            owner_id=owner_id,
            rule_id=series_id,
            after_due=skipped_due,
        )
        if not next_time:
            return _fail("Could not compute next occurrence.")

        await publish_operations_changed(owner_id, "schedules")
        return f"Skipped. Next occurrence at {format_local_when(next_time)}"

    @tool
    async def add_exception(self, series_id: str, date: str) -> str | CapabilityErrorDetail:
        """
        Block a specific date on a recurring notification series.
        Args:
            series_id: series_id from get_alerts.
            date: ISO ("2026-02-14"), month-day ("Feb 14"), or weekday ("Friday").
        """
        owner_id = get_owner_id()
        rule_doc = await _require_scheduler_rule(owner_id, series_id)
        if isinstance(rule_doc, CapabilityErrorDetail):
            return rule_doc

        trigger = rule_doc.get("origin", {})
        if trigger.get("recurrence", "").startswith("every"):
            return _fail("Date exceptions only apply to day-based recurrence.")

        iso_date = parse_date(date)
        if not iso_date:
            return _fail(f"Could not parse date '{date}'.")

        await mongodb.db.trigger_rules.update_one(
            {"id": series_id, "owner_id": owner_id},
            {"$addToSet": {"exceptions": iso_date}},
        )

        tz_name = trigger.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        pending = await mongodb.db.trigger_instances.find_one(
            {"rule_id": series_id, "status": "pending", "owner_id": owner_id}
        )
        if pending:
            due_dt = coerce_datetime(pending.get("due_at"))
            if due_dt and due_dt.astimezone(tz).date().isoformat() == iso_date:
                await mongodb.db.trigger_instances.delete_one({"_id": pending["_id"]})
                updated_rule = await mongodb.db.trigger_rules.find_one({"id": series_id})
                now = datetime.now(timezone.utc)
                next_time = next_occurrence(
                    recurrence_rule_from_origin(
                        trigger,
                        rule_doc=updated_rule,
                        owner_id=owner_id,
                        rule_id=series_id,
                    ),
                    now,
                )
                if next_time:
                    await trigger_service.materialize_recurring_occurrence(
                        owner_id=owner_id,
                        rule_id=series_id,
                        origin=TriggerOrigin.model_validate(rule_doc["origin"]),
                        action=TriggerAction.model_validate(rule_doc["action"]),
                        attention=AttentionPolicy.model_validate(rule_doc["attention"]),
                        delivery=DeliveryPlan.model_validate(rule_doc["delivery"]),
                        freshness=FreshnessPolicy.model_validate(rule_doc["freshness"]),
                        due_at=next_time,
                        management=ManagementOwnership.model_validate(rule_doc["management"]),
                    )

        await publish_operations_changed(owner_id, "schedules")
        return f"{iso_date} added as exception."

    @tool
    async def remove_exception(self, series_id: str, date: str) -> str | CapabilityErrorDetail:
        """
        Remove a date exception, reinstating that date.
        Args:
            series_id: series_id from get_alerts.
            date: ISO ("2026-02-14"), month-day ("Feb 14"), or weekday ("Friday").
        """
        owner_id = get_owner_id()
        rule_doc = await _require_scheduler_rule(owner_id, series_id)
        if isinstance(rule_doc, CapabilityErrorDetail):
            return rule_doc

        iso_date = parse_date(date)
        if not iso_date:
            return _fail(f"Could not parse date '{date}'.")

        if iso_date not in rule_doc.get("exceptions", []):
            return _fail(f"{iso_date} is not in the exceptions list.")

        await mongodb.db.trigger_rules.update_one(
            {"id": series_id, "owner_id": owner_id},
            {"$pull": {"exceptions": iso_date}},
        )
        await publish_operations_changed(owner_id, "schedules")
        return f"{iso_date} removed from exceptions."

    @tool
    async def pause_series(self, series_id: str, until: str | None = None) -> str | CapabilityErrorDetail:
        """
        Pause a recurring notification series. No occurrences fire until resumed.
        Prefer setups.pause when the user names configured behavior.
        Args:
            series_id: series_id from get_alerts().
            until: Relative ("7d") or natural date/time ("2026-02-17", "next Friday"). Omit for indefinite.
        """
        owner_id = get_owner_id()
        rule_doc = await _require_scheduler_rule(owner_id, series_id)
        if isinstance(rule_doc, CapabilityErrorDetail):
            return rule_doc

        try:
            paused_until = (
                parse_schedule_time(until, timezone_name=get_timezone(), default_time_for_date=time(9, 0))
                if until
                else None
            )
        except ValueError as exc:
            return _fail(f"Could not parse until={until!r}. {exc}.")
        await patch_rule_lifecycle(
            owner_id,
            f"rule:{series_id}",
            definition_pause_patch(paused_until),
        )
        if paused_until:
            return f"Series paused until {format_local_when(paused_until)}"
        return "Series paused indefinitely. Use resume_series to restart."

    @tool
    async def resume_series(self, series_id: str) -> str | CapabilityErrorDetail:
        """Resume a paused recurring series (series_id from get_alerts). Schedules the next occurrence."""
        owner_id = get_owner_id()
        rule_doc = await _require_scheduler_rule(owner_id, series_id)
        if isinstance(rule_doc, CapabilityErrorDetail):
            return rule_doc

        summary = await patch_rule_lifecycle(
            owner_id,
            f"rule:{series_id}",
            SetupPatch(enabled=True, paused_until=None),
        )
        if summary.next_due_at:
            return f"Series resumed. Next occurrence at {format_local_when(summary.next_due_at)}"
        return "Series resumed."


def _format_receipt_when(trigger_time: datetime, *, now: datetime | None = None) -> str:
    trigger_time = coerce_datetime(trigger_time, default=now)
    now_utc = now or datetime.now(timezone.utc)
    local_str = trigger_time.astimezone(ZoneInfo(get_timezone())).strftime("%I:%M %p").lstrip("0")
    secs = max(0, int((trigger_time - now_utc).total_seconds()))
    if secs < 60:
        return f"{local_str} · in {secs} sec"
    return f"{local_str} · in {secs // 60} min"

