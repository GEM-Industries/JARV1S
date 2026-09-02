"""Unified rule authoring facade."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from core.context import get_owner_id, get_timezone
from core.decorators import tool
from core.operations.events import publish_operations_changed
from core.plugins.result import ToolResult
from core.plugins.ui import content_envelope, receipt_envelope
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.scheduling import coerce_timezone, format_local_when, is_valid, parse_schedule_time
from core.triggers.conditions import field_conditions_from_dicts
from core.triggers.models import AttentionPolicy, DeliveryPlan, FreshnessPolicy, ManagementOwnership, TriggerAction, TriggerOrigin
from core.triggers.service import trigger_service
from core.triggers.vocabulary import DECISION_TELL, TriggerDecision
from core.plugins.capabilities import CapabilityErrorDetail
from services.database.mongodb import mongodb



def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


def _parse_origin(origin: dict[str, Any]) -> tuple[TriggerOrigin | None, datetime | None, CapabilityErrorDetail | None]:
    kind = origin.get("kind")
    if kind == "external":
        return None, None, _fail(
            "External-event rules belong on automations.create_rule. "
            "Call automations.list_available_triggers(source) then "
            "automations.create_rule; do not author them through rules.create."
        )
    if kind not in {"time", "interval"}:
        return None, None, _fail(
            "origin.kind must be one of: time, interval. "
            "External app events use automations.create_rule."
        )

    tz_name = origin.get("timezone") or get_timezone()
    fire_at = origin.get("fire_at")
    when = origin.get("when") or fire_at
    duration_s = origin.get("duration_s")
    if kind == "interval" and duration_s and not when:
        due_at = datetime.now(timezone.utc) + timedelta(seconds=int(duration_s))
    else:
        if not when:
            return None, None, _fail("time/interval rules require origin.when or origin.fire_at.")
        try:
            due_at = parse_schedule_time(str(when), timezone_name=tz_name)
        except ValueError as exc:
            return None, None, _fail(f"Could not parse origin.when={when!r}. {exc}.")

    recurrence = origin.get("recurrence")
    if recurrence:
        recurrence = str(recurrence).lower().strip()
        if not is_valid(recurrence):
            return None, None, _fail(
                f"Invalid recurrence '{recurrence}'. "
                "Use: daily, weekdays, weekends, weekly, every Xh, every Xm."
            )

    original_local_time = (
        due_at.astimezone(coerce_timezone(tz_name)).strftime("%H:%M")
        if recurrence else None
    )
    return (
        TriggerOrigin(
            kind=kind,
            fire_at=due_at,
            duration_s=duration_s,
            recurrence=recurrence,
            timezone=tz_name,
            original_local_time=original_local_time,
        ),
        due_at,
        None,
    )


_ACTION_KEYS = frozenset({
    "decision",
    "message",
    "instructions",
    "protocol_name",
    "content_type",
    "reply_grounding",
})


def _parse_action(action: dict[str, Any]) -> tuple[TriggerAction | None, CapabilityErrorDetail | None]:
    unknown = sorted(set(action) - _ACTION_KEYS)
    if unknown:
        return None, _fail(f"Unknown action field(s): {', '.join(unknown)}.")

    decision: TriggerDecision = action.get("decision") or DECISION_TELL
    try:
        parsed = TriggerAction(
            decision=decision,
            message=action.get("message") or "",
            protocol_name=action.get("protocol_name"),
            instructions=action.get("instructions"),
            content_type=action.get("content_type"),
            reply_grounding=action.get("reply_grounding") or {},
        )
    except ValueError as exc:
        return None, _fail(f"Invalid action. {exc}")
    return parsed, None


class RulesPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="rules",
        version="1.0.0",
        description="Create durable When/Do rules for time or interval triggers. External app events use automations.",
        utterances=[
            "create a routine",
            "set up a daily routine",
            "create a conditional routine",
            "every morning do this",
            "make this happen on a schedule",
            "set up a rule",
            "set up a durable when do rule",
        ],
    )

    @tool
    async def create(
        self,
        name: str,
        origin: dict[str, Any],
        action: dict[str, Any],
        conditions: list[dict[str, Any]] | None = None,
        attention: Literal["normal", "urgent", "critical"] = "normal",
        sound: Literal["none", "chime", "timer", "alarm"] = "chime",
        confirmed: bool = False,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Create one durable rule: When origin fires, optionally gate with conditions, then run action.
        Use directly for clear persistent routines. Time side-effects should use decision="act" with instructions; user-facing reminders use tell; conditional prompts use offer. For offer actions, instructions should state the real decision boundary: what means speak now, defer, or suppress as already handled/no longer useful. For broad setup, agents.dispatch(mode="jarvis") may call this tool and must persist the rule before reporting success.
        origin: {"kind": "time|interval", "when": "7:30am", "recurrence": "daily"}. External app events use automations.create_rule.
        action: {"decision": "tell|offer|act", "message": "...", "instructions": "...", "protocol_name": "..."}.
        confirmed: false previews; call again with confirmed=true to persist.
        Edit recurring time rules in place with scheduler.replace_alert(query=..., scope="series"); do not create a duplicate replacement.
        """
        owner_id = get_owner_id()
        if confirmed:
            normalized_name = name.strip()
            existing = await mongodb.db.trigger_rules.find_one(
                {
                    "owner_id": owner_id,
                    "name": re.compile(f"^{re.escape(normalized_name)}$", re.IGNORECASE),
                    "surface": True,
                },
                {"_id": 0, "id": 1, "name": 1, "origin.kind": 1},
            )
            if existing and existing.get("origin", {}).get("kind") == "external":
                return _fail(
                    f"Rule {existing.get('name')!r} already exists "
                    f"(rule_id={existing['id']!r}). Update it with automations.update_rule; "
                    "do not create a duplicate."
                )
            if existing:
                return _fail(
                    f"Scheduled rule {existing.get('name')!r} already exists "
                    f"(series_id={existing['id']!r}). Update it with "
                    f"scheduler.replace_alert(query={existing.get('name')!r}, "
                    "scope='series', ...); "
                    "do not create a duplicate."
                )

        parsed_origin, first_due, origin_error = _parse_origin(origin)
        if origin_error or parsed_origin is None:
            return origin_error or _fail("Invalid origin.")

        parsed_action, action_error = _parse_action(action)
        if action_error or parsed_action is None:
            return action_error or _fail("Invalid action.")
        plan = DeliveryPlan()
        policy = AttentionPolicy(level=attention, sound=sound)
        freshness = FreshnessPolicy(
            stale_if_source_event_started=(
                parsed_origin.source == "calendar" and parsed_origin.offset_minutes < 0
            )
        )
        parsed_conditions = field_conditions_from_dicts(conditions)

        if not confirmed:
            sections = [
                {
                    "type": "kv",
                    "pairs": {
                        "Name": name,
                        "Origin": parsed_origin.model_dump(mode="json"),
                        "Action": parsed_action.model_dump(mode="json"),
                        "Attention": attention,
                    },
                },
                {
                    "type": "markdown",
                    "content": "Preview only. Call `jarvis.rules.create(..., confirmed=True)` with the same fields to persist.",
                },
            ]
            return ToolResult(
                content=f"Preview only. Rule '{name}' was not created.",
                ui=[content_envelope("Rule Preview", sections)],
            )

        rule = await trigger_service.create_rule(
            owner_id=owner_id,
            name=name,
            origin=parsed_origin,
            conditions=parsed_conditions,
            action=parsed_action,
            attention=policy,
            delivery=plan,
            freshness=freshness,
            management=ManagementOwnership(
                provider="automations" if parsed_origin.kind == "external" else "scheduler",
            ),
        )
        if first_due is not None:
            await trigger_service.create_instance(
                owner_id=owner_id,
                rule_id=rule.id,
                origin=parsed_origin,
                action=parsed_action,
                attention=policy,
                delivery=plan,
                freshness=rule.freshness,
                due_at=first_due,
                management=rule.management,
            )

        scope = "automations" if parsed_origin.kind == "external" else "schedules"
        await publish_operations_changed(owner_id, scope)
        sublabel = (
            f"Starts {format_local_when(first_due)}"
            if first_due else f"{parsed_origin.source}.{parsed_origin.event or '*'}"
        )
        return ToolResult(
            content=f"Rule '{name}' created. ID: {rule.id}",
            ui=[receipt_envelope("Rule", name, sublabel=sublabel)],
        )
