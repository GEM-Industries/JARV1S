"""
Automations Plugin — LLM-facing tools for creating and managing automation rules.

Rules are stored as external-origin TriggerRule records and evaluated by the
AutomationService background daemon, which creates TriggerInstance documents
when rule conditions are met.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.context import get_owner_id, get_tz
from core.decorators import tool
from core.operations.events import publish_operations_changed
from core.operations.projection import resolve_managed_setup
from core.operations.setups import SetupPatch, patch_rule_lifecycle
from core.plugins.mutations import merge_model_patch, validation_error_message
from core.plugins.result import ToolResult
from core.plugins.ui import content_envelope, receipt_envelope
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.time import coerce_datetime, parse_duration
from core.triggers.conditions import (
    field_condition_dicts,
    field_conditions_from_dicts,
)
from core.triggers.models import AttentionPolicy, DeliveryPlan, FreshnessPolicy, ManagementOwnership, TriggerAction, TriggerOrigin, TriggerRule
from core.triggers.lifecycle import cancel_open_instances_for_rule
from core.triggers.priority import AttentionLevel, attention_policy_fields
from core.triggers.service import trigger_service
from core.triggers.vocabulary import DECISION_TELL, TriggerDecision
from core.plugins.capabilities import CapabilityErrorDetail
from services.database.mongodb import mongodb


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


def _trigger_origin(trigger: dict[str, Any]) -> TriggerOrigin:
    return TriggerOrigin(
        kind="external",
        source=trigger.get("source"),
        event=trigger.get("event") or "",
        offset_minutes=int(trigger.get("offset", 0) or 0),
    )


def _trigger_action(action: dict[str, Any]) -> TriggerAction:
    return TriggerAction(
        decision=action.get("decision", DECISION_TELL),
        message=action.get("message") or "Automation triggered.",
        protocol_name=action.get("protocol"),
        instructions=action.get("instructions"),
    )


def _attention_policy(importance: AttentionLevel = "normal") -> AttentionPolicy:
    return AttentionPolicy(**attention_policy_fields(importance))


def _delivery_plan() -> DeliveryPlan:
    return DeliveryPlan()


def _freshness_policy(trigger: dict[str, Any], action: dict[str, Any]) -> FreshnessPolicy:
    if trigger.get("source") != "calendar":
        return FreshnessPolicy()

    offset = int(trigger.get("offset", 0) or 0)
    return FreshnessPolicy(stale_if_source_event_started=offset < 0)


def _automation_action_view(rule: TriggerRule) -> dict[str, Any]:
    return {
        "decision": rule.action.decision,
        "message": rule.action.message,
        "protocol": rule.action.protocol_name,
        "instructions": rule.action.instructions,
    }


def _automation_trigger_view(rule: TriggerRule) -> dict[str, Any]:
    return {
        "source": rule.origin.source or "",
        "event": rule.origin.event or "",
        "offset": rule.origin.offset_minutes,
    }


def _automation_rule_model(rule_doc: dict[str, Any] | TriggerRule) -> AutomationRule:
    rule = rule_doc if isinstance(rule_doc, TriggerRule) else TriggerRule.model_validate(rule_doc)
    return AutomationRule(
        id=rule.id,
        name=rule.name,
        enabled=rule.enabled,
        importance=rule.attention.level,
        trigger=_automation_trigger_view(rule),
        conditions=field_condition_dicts(rule.conditions),
        action=_automation_action_view(rule),
        paused_until=rule.paused_until,
        suppressed_events=rule.suppressed_event_ids,
        created_at=rule.created_at,
    )


# ---------------------------------------------------------------------------
# Module-level helpers (not LLM-facing)
# ---------------------------------------------------------------------------

async def _resolve_slug(source: str, event: str) -> tuple[str | None, list[str]]:
    """Resolve a derived (source, event) pair back to the Composio trigger slug.

    Returns (slug, valid_events) where:
      slug        — matched Composio slug, or None if no match
      valid_events — list of valid derived event names for this source (empty if source has no Composio triggers)
    """
    from core.integrations.composio_gateway import get_composio_gateway
    gateway = get_composio_gateway()
    if not gateway:
        return None, []
    triggers = await gateway.list_trigger_types(source)
    if not triggers:
        return None, []
    from core.integrations.composio_webhooks import derive_source_event
    valid_events = []
    for t in triggers:
        slug = t.get("slug") or t.get("name", "")
        if not slug:
            continue
        s, e = derive_source_event(slug)
        valid_events.append(e)
        if (s, e) == (source, event):
            return slug, valid_events
    return None, valid_events


async def _deregister_push_trigger(source: str, event: str) -> None:
    """Deregister a Composio push trigger for (source, event) if one exists and is registered."""
    slug, _ = await _resolve_slug(source, event)
    if not slug:
        return
    from core.integrations.composio_gateway import get_composio_gateway
    gateway = get_composio_gateway()
    if gateway:
        await gateway.deregister_trigger(source, slug)


async def delete_automation_rule(owner_id: str, rule_id: str) -> TriggerRule | None:
    """Delete one external automation and all pending delivery artifacts."""
    rule_doc = await mongodb.db.trigger_rules.find_one(
        {"id": rule_id, "owner_id": owner_id, "origin.kind": "external"},
    )
    if not rule_doc:
        return None
    rule = TriggerRule.model_validate(rule_doc)
    await mongodb.db.trigger_rules.update_one(
        {"id": rule_id, "owner_id": owner_id, "origin.kind": "external"},
        {"$set": {"enabled": False, "updated_at": datetime.now(timezone.utc)}},
    )
    await cancel_open_instances_for_rule(owner_id, rule_id, reason="user_deleted")
    result = await mongodb.db.trigger_rules.delete_one(
        {"id": rule_id, "owner_id": owner_id, "origin.kind": "external"},
    )
    if result.deleted_count != 1:
        return None
    source = rule.origin.source or ""
    event = rule.origin.event or ""
    if source and event:
        await _deregister_push_trigger(source, event)
    await mongodb.db.automation_fired.delete_many({"rule_id": rule_id})
    await publish_operations_changed(owner_id, "automations")
    return rule


async def _resolve_automation_rule_id(
    owner_id: str,
    token: str,
) -> str | CapabilityErrorDetail:
    needle = token.strip()
    if not needle:
        return _fail(f"No automation matching {needle!r}.")
    # Exact ids are already on trigger_rules; skip the catalog scan.
    existing = await mongodb.db.trigger_rules.find_one(
        {"id": needle, "owner_id": owner_id, "origin.kind": "external"},
    )
    if existing:
        return needle
    resolved = await resolve_managed_setup(owner_id, needle, setup_type="automation")
    if isinstance(resolved, list):
        candidates = ", ".join(
            f"{row.name} ({row.resource_ref})" for row in resolved[:8]
        )
        return _fail(f"Ambiguous automation. Retry with one resource_ref: {candidates}")
    if resolved is not None:
        return resolved.rule_id or resolved.resource_id
    return _fail(f"No automation matching {needle!r}.")


def _interval_trigger_error(trigger: dict[str, Any]) -> CapabilityErrorDetail | None:
    if (
        trigger.get("source") == "time"
        or trigger.get("source") == "scheduler"
        or trigger.get("event") == "interval"
        or "interval" in trigger
        or "interval_minutes" in trigger
    ):
        return _fail(
            "create_rule is for external event sources, not time intervals. "
            "Use scheduler.remind(when='45m', message='...', recurrence='every 45m', "
            "instructions='only if ...') for recurring reminders."
        )
    return None


def _condition_key(condition: dict[str, Any]) -> tuple[str, str, str]:
    return (condition["field"], condition["op"], condition["value"])


def _validate_condition_list(raw: list[dict[str, Any]]) -> list[dict[str, Any]] | CapabilityErrorDetail:
    try:
        return [ConditionConfig.model_validate(c).model_dump() for c in raw]
    except ValidationError as error:
        return validation_error_message("condition", ConditionConfig, error)


def _condition_field_error(source: str, conditions: list[dict[str, Any]]) -> CapabilityErrorDetail | None:
    """Reject invented fields for built-in watchers that publish a closed field list."""
    if not source or not conditions:
        return None
    from services.automation import automation_service

    condition_fields = automation_service.watcher_condition_fields(source)
    if not condition_fields:
        return None

    valid_fields = sorted(
        str(field.get("field"))
        for field in condition_fields
        if field.get("field")
    )
    invalid_fields = sorted(
        {str(condition.get("field")) for condition in conditions}
        - set(valid_fields)
    )
    if not invalid_fields:
        return None

    return _fail(
        f"Invalid condition field(s) for source='{source}': {invalid_fields}. "
        f"Use one of: {valid_fields}. Call list_available_triggers('{source}') "
        "to inspect condition_fields. If the policy cannot be expressed with those "
        "fields, use decision='offer' or decision='act' with instructions instead."
    )


def _apply_condition_patches(
    existing: list[dict[str, Any]],
    *,
    add: list[dict[str, Any]] | None,
    remove: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | CapabilityErrorDetail:
    current = list(existing)
    if add:
        validated = _validate_condition_list(add)
        if isinstance(validated, CapabilityErrorDetail):
            return validated
        seen = {_condition_key(c) for c in current}
        for condition in validated:
            key = _condition_key(condition)
            if key not in seen:
                current.append(condition)
                seen.add(key)
    if remove:
        validated = _validate_condition_list(remove)
        if isinstance(validated, CapabilityErrorDetail):
            return validated
        remove_keys = {_condition_key(c) for c in validated}
        current = [c for c in current if _condition_key(c) not in remove_keys]
    return current


def _event_hint(event: str, valid_events: list[str]) -> str:
    if event.startswith("event_"):
        stripped = event.removeprefix("event_")
        if stripped in valid_events:
            return f" Use event='{stripped}'."
    return ""


def _builtin_trigger_error(source: str, event: str) -> str | None:
    if not source or not event:
        return None
    from services.automation import automation_service

    valid_events = [
        item.get("event", "")
        for item in automation_service.watcher_trigger_info(source)
        if item.get("event")
    ]
    if not valid_events or event in valid_events:
        return None
    return _fail(
        f"event='{event}' is not valid for source='{source}'. "
        f"Valid built-in events are: {valid_events}."
        f"{_event_hint(event, valid_events)} "
        f"Call list_available_triggers('{source}') and recreate the rule with an exact event value."
    )


def _unknown_source_error(source: str) -> str | None:
    if not source:
        return _fail("trigger.source is required. Call list_available_triggers(source) first.")
    from services.automation import automation_service

    if source in automation_service.watcher_sources():
        return None
    # Tests and optional push-backed sources may not have live watchers registered
    # when this tool is called, but internal/time sources are always invalid here.
    if source in {"scheduler", "time", "interval", "cron", "system", "manual"}:
        return _fail(
            f"source='{source}' is not an external event source. "
            "Use scheduler tools for time-based rules."
        )
    return None


async def _auto_register_push_trigger(source: str, event: str) -> str | None:
    """Register the Composio push trigger for (source, event) if one exists.

    Returns:
      "ok"  — trigger found and registered successfully
      None  — bespoke watcher handles this source, or no Composio triggers — silent
      str   — actionable error: wrong event name or registration failure
    """
    from core.plugins.registry import registry
    if source in registry.bespoke_names:
        return None  # bespoke watcher owns this source — no Composio involvement

    slug, valid_events = await _resolve_slug(source, event)
    if not valid_events:
        return None  # poll source or Composio not configured — no action needed
    if not slug:
        return (
            f"Warning: event='{event}' not found for source='{source}'. "
            f"Valid events are: {valid_events}. "
            f"Delete this rule, call list_available_triggers('{source}'), and recreate with the correct event name."
        )
    from core.integrations.composio_gateway import get_composio_gateway
    gateway = get_composio_gateway()
    ok = await gateway.register_trigger(source, slug)
    if not ok:
        return f"Warning: push trigger found but registration failed — is '{source}' connected?"
    return "ok"


# ---------------------------------------------------------------------------
# Sub-models — typed for LLM schema visibility via CapabilityDefinition
# ---------------------------------------------------------------------------

# `extra="forbid"` on all three sub-models — unknown keys (e.g. `channel_id` on
# trigger, `type`/`prompt` on action, typoed `op` on condition) raise ValidationError
# instead of silently persisting as a broken rule.

class TriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str         # "calendar", "gmail", "slack", "github" etc.
    event: str = ""     # derived event name: "starting"/"ending" (calendar), or derived from Composio
                        # slug e.g. "new_gmail_message", "email_sent_trigger", "receive_message".
                        # Use list_available_triggers(source) to discover names for external apps.
                        # Leave empty to match any event from this source.
    offset: int = 0     # minutes offset from event start (poll path only; negative = before)


ConditionOp = Literal[
    "contains", "not_contains", "equals", "not_equals", "greater_than", "less_than"
]
class ConditionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str          # e.g. "title", "location", "duration_minutes", "attendee_count", "is_all_day"
    op: ConditionOp
    value: str


class ActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: TriggerDecision = DECISION_TELL
    message: str = "Automation triggered."  # {field} placeholders resolved from source item
    protocol: Optional[str] = None          # protocol name to execute
    # Fire-time work and policy interpreted by the agent alongside rule_id —
    # for lifecycle / conditional behavior the structured fields cannot capture.
    instructions: Optional[str] = None


class AutomationRule(BaseModel):
    id: str
    name: str
    enabled: bool
    importance: AttentionLevel
    trigger: TriggerConfig
    conditions: list[ConditionConfig]
    action: ActionConfig
    paused_until: Optional[datetime] = None
    suppressed_events: list[str] = []
    created_at: datetime


class TriggerInfo(BaseModel):
    source: str       # use as trigger.source in create_rule
    event: str        # use as trigger.event in create_rule — exact value required
    description: str
    provider: str     # "built-in" (works immediately) | "composio" (requires connect_integration first)
    condition_fields: list[dict[str, Any]] = Field(default_factory=list)


def _automation_preview(
    *,
    name: str,
    trigger: dict[str, Any],
    action: dict[str, Any],
    conditions: list[dict[str, Any]],
    importance: AttentionLevel,
) -> ToolResult:
    trigger_label = f"{trigger.get('source', '')}.{trigger.get('event', '') or '*'}"
    condition_text = "All matching events" if not conditions else "; ".join(
        f"{c['field']} {c['op']} {c['value']}" for c in conditions
    )
    action_summary = action.get("message") or action.get("protocol") or "Automation triggered."
    sections = [
        {
            "type": "kv",
            "pairs": {
                "Name": name,
                "Trigger": trigger_label,
                "Offset": f"{trigger.get('offset', 0)} minutes",
                "Conditions": condition_text,
                "Decision": action.get("decision", DECISION_TELL),
                "Importance": importance,
            },
        },
        {
            "type": "markdown",
            "content": (
                f"Message: {action_summary}\n\n"
                f"Instructions: {action.get('instructions') or 'None'}\n\n"
                "If this is correct, call `jarvis.automations.create_rule(..., confirmed=True)` with the same fields."
            ),
        },
    ]
    return ToolResult(
        content=(
            f"Preview only. Automation '{name}' was not created. "
            "Confirm before persisting this recurring behavior."
        ),
        ui=[content_envelope("Automation Preview", sections)],
    )


class AutomationsPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="automations",
        version="1.0.0",
        description="Create event-based rules: notify, run protocols, or act when external events match conditions.",
        utterances=[
            "set up an automation",
            "let me know when something happens",
            "tell me when something happens",
            "watch for something and tell me",
            "when I get an email, let me know",
            "tell me when someone mentions me on Slack",
            "alert me when I get a GitHub pull request",
            "pause my automations",
            "turn my automations back on",
            "what can you watch for",
        ],
    )

    @tool
    async def create_rule(
        self,
        name: str,
        trigger: dict[str, Any],
        action: dict[str, Any],
        conditions: Optional[list[dict[str, Any]]] = None,
        importance: AttentionLevel = "normal",
        instructions: Optional[str] = None,
        confirmed: bool = False,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Create an event-based automation rule. Use for "notify/alert/remind me when..." requests tied to external app/provider events; use scheduler tools for explicit times.
        For intervals ("every 45 minutes", "in 2 hours", "tomorrow at 9"), use scheduler.defer for side effects or scheduler.remind for user-facing messages.
        ALWAYS call list_available_triggers(source) first and use the exact source/event returned; do not guess trigger names. Use setups.find(setup_type="automation") to inspect existing rules before creating duplicates.
        trigger: {"source": "<source>", "event": "<event from list_available_triggers>", "offset": 0}
          offset: minutes relative to event time for anticipated sources only, e.g. -5 = 5 min before.
          Use negative offsets for pre-start reminders; use offset=0 with decision="act" for side effects that should run when/during the event.
        conditions: [{"field": "<field>", "op": "<op>", "value": "<value>"}] are AND-ed prefilters using condition_fields from list_available_triggers.
          Use contains/not_contains/equals/not_equals for strings, greater_than/less_than for numbers, and equals "true"/"false" for booleans.
          Do not invent fields. For OR, semantic matching ("looks urgent"), unavailable fields, time windows, or lifecycle logic, leave conditions empty and use decision="offer" or decision="act" with instructions.
        action: {"decision": "tell|offer|act", "message": "<brief intent>", "protocol": null, "instructions": null}.
          `instructions` may also be passed as a top-level argument; it is folded into action.instructions.
          decision: "tell" announces matching events; "offer" evaluates at fire time and speaks only if useful; "act" does fire-time work headless and stays silent.
          protocol: existing protocol name from setups.find(setup_type="protocol"), or null.
          Reactive triggers (e.g. gmail/slack/github): write a short intent; the full event payload is passed at fire time for a richer response.
          Anticipated triggers (e.g. calendar): use {field} placeholders, e.g. "Meeting '{title}' starts in {offset} minutes".
          instructions: fire-time policy or work to perform; for decision="offer", state what evidence means speak now, defer, or suppress because it is already handled or no longer useful. The agent receives rule_id and may call update_rule/delete_rule/suppress_event/pause_all or other jarvis.* tools if needed.
        importance: how hard matching events should try to reach the user.
          "normal" — announced when active; deferred during quiet/paused.
          "urgent" — breaks through quiet hours.
          "critical" — highest tier; still deferred while paused until safety-class origins exist.
        confirmed: false returns a preview only and does not persist. Call again with confirmed=true after the user approves the preview.
        Fired rules create TriggerInstance rows and can retry voice delivery if the user is offline.
        """
        interval_error = _interval_trigger_error(trigger)
        if interval_error:
            return interval_error

        if instructions and not action.get("instructions"):
            action = {**action, "instructions": instructions}

        try:
            trigger = TriggerConfig.model_validate(trigger).model_dump()
        except ValidationError as error:
            return validation_error_message("trigger", TriggerConfig, error)
        try:
            action = ActionConfig.model_validate(action).model_dump()
        except ValidationError as error:
            return validation_error_message("action", ActionConfig, error)
        source_error = _unknown_source_error(trigger.get("source", ""))
        if source_error:
            return source_error
        builtin_error = _builtin_trigger_error(trigger.get("source", ""), trigger.get("event", ""))
        if builtin_error:
            return builtin_error

        owner_id = get_owner_id()
        protocol_name = action.get("protocol")
        if protocol_name:
            from plugins.protocol import protocol_exists
            if not await protocol_exists(protocol_name, owner_id):
                return _fail(
                    f"Protocol '{protocol_name}' not found. Use setups.find(setup_type='protocol') "
                    "to choose an existing protocol, or set action.instructions for one-off live work."
                )
        try:
            validated_conditions = [
                ConditionConfig.model_validate(condition).model_dump()
                for condition in conditions or []
            ]
        except ValidationError as error:
            return validation_error_message("condition", ConditionConfig, error)
        condition_error = _condition_field_error(trigger.get("source", ""), validated_conditions)
        if condition_error:
            return condition_error

        if not confirmed:
            return _automation_preview(
                name=name,
                trigger=trigger,
                action=action,
                conditions=validated_conditions,
                importance=importance,
            )

        rule = await trigger_service.create_rule(
            owner_id=owner_id,
            name=name,
            origin=_trigger_origin(trigger),
            conditions=field_conditions_from_dicts(validated_conditions),
            action=_trigger_action(action),
            attention=_attention_policy(importance),
            delivery=_delivery_plan(),
            freshness=_freshness_policy(trigger, action),
            management=ManagementOwnership(provider="automations", resource_id=None),
        )
        await publish_operations_changed(owner_id, "automations")

        source = trigger.get("source", "")
        event = trigger.get("event", "")
        reg_result = await _auto_register_push_trigger(source, event) if source and event else None

        msg = f"Automation '{name}' created (id: {rule.id})."
        if reg_result == "ok":
            msg += " Push trigger registered — fires when Composio delivers a webhook."
        elif reg_result is not None:
            msg += f" {reg_result}"
        else:
            msg += " Use test_rule to verify against current data."
        disable_path = f"Disable with jarvis.automations.update_rule(rule_id={rule.id!r}, enabled=False)."
        trigger_label = f"{trigger.get('source', '')}.{trigger.get('event', '')}"
        action_label = action.get("message") or action.get("protocol") or "Automation"
        return ToolResult(
            content=f"{msg} {disable_path}",
            ui=[receipt_envelope(
                "Automation",
                f"{name} · {trigger_label}",
                sublabel=str(action_label),
            )],
        )

    @tool
    async def update_rule(
        self,
        rule_id: str,
        name: Optional[str] = None,
        enabled: Optional[bool] = None,
        trigger: Optional[dict[str, Any]] = None,
        conditions: Optional[list[dict[str, Any]]] = None,
        add_conditions: Optional[list[dict[str, Any]]] = None,
        remove_conditions: Optional[list[dict[str, Any]]] = None,
        action: Optional[dict[str, Any]] = None,
        importance: Optional[AttentionLevel] = None,
        paused_until: Optional[str] = None,
        instructions: Optional[str] = None,
    ) -> str | CapabilityErrorDetail:
        """
        Modify an existing event automation rule. Only provided fields are updated.
        For small filter tweaks use add_conditions/remove_conditions; use conditions=[] to clear all.
        Conditions may only use condition_fields from list_available_triggers for the rule source; do not invent fields.
        Use action={"instructions": "..."} or instructions=... for fire-time policy when the rule needs semantic judgment, unavailable fields, lifecycle behavior, or side-effect work; pass instructions="" to clear.
        paused_until: ISO datetime to pause until; use unpause_rule to resume immediately.
        rule_id accepts the create_rule id or a unique automation name.
        """
        owner_id = get_owner_id()
        resolved = await _resolve_automation_rule_id(owner_id, rule_id)
        if isinstance(resolved, CapabilityErrorDetail):
            return resolved
        rule_id = resolved
        updates: dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

        if name is not None:
            updates["name"] = name
        if enabled is not None:
            updates["enabled"] = enabled

        needs_existing = any(
            value is not None
            for value in (trigger, action, conditions, add_conditions, remove_conditions, instructions)
        )
        existing: dict[str, Any] | None = None
        existing_rule = await mongodb.db.trigger_rules.find_one(
            {"id": rule_id, "owner_id": owner_id, "origin.kind": "external"},
        )
        if not existing_rule:
            return _fail(f"No rule found with id '{rule_id}'.")

        rule_model = TriggerRule.model_validate(existing_rule)
        if needs_existing or paused_until is not None or importance is not None:
            existing = _automation_rule_model(rule_model).model_dump()

        if trigger is not None:
            try:
                merged_trigger = merge_model_patch(TriggerConfig, existing.get("trigger"), trigger)
            except ValidationError as error:
                return validation_error_message("trigger", TriggerConfig, error)
            source_error = _unknown_source_error(merged_trigger.get("source", ""))
            if source_error:
                return source_error
            builtin_error = _builtin_trigger_error(
                merged_trigger.get("source", ""),
                merged_trigger.get("event", ""),
            )
            if builtin_error:
                return builtin_error
            updates["origin"] = _trigger_origin(merged_trigger).model_dump(mode="python")
        if conditions is not None:
            rule_trigger = (
                _automation_trigger_view(TriggerRule.model_validate({**existing_rule, **updates}))
                if "origin" in updates else existing.get("trigger") or {}
            )
            rule_source = rule_trigger.get("source", "")
            validated_conditions = _validate_condition_list(conditions)
            if isinstance(validated_conditions, CapabilityErrorDetail):
                return validated_conditions
            condition_error = _condition_field_error(rule_source, validated_conditions)
            if condition_error:
                return condition_error
            updates["conditions"] = [
                condition.model_dump(mode="python")
                for condition in field_conditions_from_dicts(validated_conditions)
            ]
        elif add_conditions is not None or remove_conditions is not None:
            rule_trigger = (
                _automation_trigger_view(TriggerRule.model_validate({**existing_rule, **updates}))
                if "origin" in updates else existing.get("trigger") or {}
            )
            rule_source = rule_trigger.get("source", "")
            if add_conditions:
                validated_add = _validate_condition_list(add_conditions)
                if isinstance(validated_add, CapabilityErrorDetail):
                    return validated_add
                condition_error = _condition_field_error(rule_source, validated_add)
                if condition_error:
                    return condition_error
            patched = _apply_condition_patches(
                existing.get("conditions", []),
                add=add_conditions,
                remove=remove_conditions,
            )
            if isinstance(patched, CapabilityErrorDetail):
                return patched
            updates["conditions"] = [
                condition.model_dump(mode="python")
                for condition in field_conditions_from_dicts(patched)
            ]

        action_patch = dict(action) if action is not None else {}
        if instructions is not None and "instructions" not in action_patch:
            action_patch["instructions"] = instructions or None
        if action_patch:
            try:
                action_update = merge_model_patch(ActionConfig, existing.get("action"), action_patch)
            except ValidationError as error:
                return validation_error_message("action", ActionConfig, error)
            updates["action"] = _trigger_action(action_update).model_dump(mode="python")
            updates["attention"] = _attention_policy(
                importance if importance is not None else existing.get("importance", "normal"),
            ).model_dump(mode="python")
            updates["delivery"] = _delivery_plan().model_dump(mode="python")
        elif importance is not None:
            updates["attention"] = _attention_policy(importance).model_dump(mode="python")
        if paused_until is not None:
            updates["paused_until"] = coerce_datetime(paused_until)

        policy_updates = {key: value for key, value in updates.items() if key != "updated_at"}
        if policy_updates and set(policy_updates) <= {"enabled", "paused_until"}:
            await patch_rule_lifecycle(
                owner_id,
                f"rule:{rule_id}",
                SetupPatch.model_validate(policy_updates),
            )
            return f"Rule '{rule_id}' updated."

        result = await mongodb.db.trigger_rules.update_one(
            {"id": rule_id, "owner_id": owner_id, "origin.kind": "external"},
            {"$set": updates},
        )
        if result.matched_count == 0:
            return _fail(f"No rule found with id '{rule_id}'.")
        await publish_operations_changed(owner_id, "automations")
        return f"Rule '{rule_id}' updated."

    @tool
    async def unpause_rule(self, rule_id: str) -> str | CapabilityErrorDetail:
        """Remove the pause from a specific rule, resuming it immediately."""
        owner_id = get_owner_id()
        resolved = await _resolve_automation_rule_id(owner_id, rule_id)
        if isinstance(resolved, CapabilityErrorDetail):
            return resolved
        rule_id = resolved
        try:
            await patch_rule_lifecycle(
                owner_id,
                f"rule:{rule_id}",
                SetupPatch.model_validate({"paused_until": None}),
            )
        except ValueError:
            return _fail(f"No rule found with id '{rule_id}'.")
        return f"Rule '{rule_id}' unpaused."

    @tool
    async def delete_rule(self, rule_id: str) -> str | CapabilityErrorDetail:
        """
        Delete an external-event automation permanently.
        rule_id accepts the create_rule id or a unique automation name.
        For other configured behavior, use setups.delete.
        """
        owner_id = get_owner_id()
        resolved = await _resolve_automation_rule_id(owner_id, rule_id)
        if isinstance(resolved, CapabilityErrorDetail):
            return resolved
        rule_id = resolved
        rule = await delete_automation_rule(owner_id, rule_id)
        if rule is None:
            return _fail(f"No automation matching {rule_id!r}.")
        return f"Rule '{rule_id}' deleted."

    @tool
    async def suppress_event(self, rule_id: str, event_id: str) -> str | CapabilityErrorDetail:
        """
        Suppress a specific event instance for a rule — it will not trigger an alert.
        Use when the user says "don't remind me about this specific occurrence."
        Get event_id from calendar.get_events() or from the context of a fired alert.
        """
        owner_id = get_owner_id()
        resolved = await _resolve_automation_rule_id(owner_id, rule_id)
        if isinstance(resolved, CapabilityErrorDetail):
            return resolved
        rule_id = resolved
        result = await mongodb.db.trigger_rules.update_one(
            {"id": rule_id, "owner_id": owner_id, "origin.kind": "external"},
            {"$addToSet": {"suppressed_event_ids": event_id}},
        )
        if result.matched_count == 0:
            return _fail(f"No rule found with id '{rule_id}'.")
        return f"Event '{event_id}' suppressed for rule '{rule_id}'."

    @tool
    async def test_rule(self, rule_id: str) -> str | CapabilityErrorDetail:
        """
        Dry-run a rule against live data. Returns a pre-formatted summary of what would fire.
        Polling sources: shows upcoming matches with fire times. Push-triggered rules: confirms registration only.
        VOICE: "That rule would fire for your 10am Standup today."
        """
        from services.automation import automation_service

        owner_id = get_owner_id()
        resolved = await _resolve_automation_rule_id(owner_id, rule_id)
        if isinstance(resolved, CapabilityErrorDetail):
            return resolved
        rule_id = resolved
        results = await automation_service.test_rule(rule_id)
        hold = automation_service.pause_observation()

        if results is None:
            rule = await mongodb.db.trigger_rules.find_one({
                "id": rule_id,
                "origin.kind": "external",
            })
            if not rule:
                return _fail(f"No rule found with id '{rule_id}'.")
            rule_model = TriggerRule.model_validate(rule)
            source = rule_model.origin.source or "unknown"
            event = rule_model.origin.event or ""
            event_label = f" ({event})" if event else ""
            body = (
                f"This is a push-delivered trigger for '{source}'{event_label}. "
                "It fires when Composio delivers a webhook — no dry-run is available. "
                "The trigger is registered and will fire automatically when the event occurs."
            )
            return f"{hold}\n{body}" if hold else body

        if not results:
            body = "No events match this rule in the current 24-hour window."
            return f"{hold}\n{body}" if hold else body

        tz = get_tz()
        now_local = datetime.now(timezone.utc).astimezone(tz)
        today = now_local.date()

        lines = [f"This rule would fire for {len(results)} event(s):"]
        for r in results:
            fire_utc = datetime.fromisoformat(r["fire_time"])
            fire_local = fire_utc.astimezone(tz)
            fire_date = fire_local.date()

            if fire_date == today:
                day = "today"
            elif fire_date == today + timedelta(days=1):
                day = "tomorrow"
            else:
                day = fire_local.strftime("%A %-d %b")

            time_str = fire_local.strftime("%I:%M %p").lstrip("0")

            seconds = r["seconds_until_fire"]
            if seconds > 0:
                mins = int(seconds // 60)
                relative = f"in {mins} min" if mins < 60 else f"in {mins // 60}h {mins % 60}m"
            else:
                relative = "fire time has passed"

            fired = " — already fired this cycle" if r.get("already_fired") else ""
            lines.append(f"  - {r['title']} {day} at {time_str} ({relative}){fired}")

        body = "\n".join(lines)
        return f"{hold}\n{body}" if hold else body

    @tool
    async def pause_all(self, duration_minutes: Optional[int | str] = None) -> str | CapabilityErrorDetail:
        """
        Globally pause all external automations. Does not pause scheduler lights,
        alarms, or a named subset — use setups.pause for those.
        duration_minutes: minutes or duration like "30m"/"2h"; omit for indefinite.
        Use resume_all to re-enable.
        """
        from services.automation import automation_service

        duration_mins: int | None = None
        if isinstance(duration_minutes, str):
            now = datetime.now(timezone.utc)
            parsed = parse_duration(duration_minutes, now=now)
            if parsed is None:
                return _fail(f"Invalid duration_minutes={duration_minutes!r}. Use minutes or a duration like '30m' or '2h'.")
            duration_mins = max(1, int(round((parsed - now).total_seconds() / 60)))
        elif duration_minutes:
            duration_mins = duration_minutes

        until = datetime.now(timezone.utc) + timedelta(minutes=duration_mins) if duration_mins else None
        await automation_service.pause(until=until)
        if until:
            return f"All automations paused for {duration_mins} minutes (until {until.isoformat()})."
        return "All automations paused indefinitely. Use resume_all to re-enable."

    @tool
    async def resume_all(self) -> str | CapabilityErrorDetail:
        """Resume all automations after a global pause."""
        from services.automation import automation_service

        await automation_service.resume()
        return "All automations resumed."

    @tool
    async def list_available_triggers(self, app_name: str) -> list[TriggerInfo]:
        """
        List trigger types and condition fields available to author a new rule for a source.
        This is not an inventory of existing automations; use setups.find for configured rules.
        Built-in triggers include condition_fields for structured filters in create_rule/update_rule.

        provider="built-in": handled by JARV1S directly — works immediately, no connection needed.
        provider="composio": requires the app to be connected first via connect_integration.

        RULE: if built-in triggers exist for this source, ALWAYS prefer them over composio.
        Built-in triggers are more reliable and don't require external connections.

        After inspecting the result, call create_rule with the exact source and event values.
        app_name: e.g. "gmail", "slack", "github".
        """
        from core.plugins.registry import registry
        from services.automation import automation_service

        results: list[TriggerInfo] = []
        condition_fields = automation_service.watcher_condition_fields(app_name)

        # Built-in watcher triggers always take precedence.
        watcher_events = automation_service.watcher_trigger_info(app_name)
        for te in watcher_events:
            results.append(TriggerInfo(
                source=app_name,
                event=te.get("event", ""),
                description=te.get("description", ""),
                provider="built-in",
                condition_fields=condition_fields,
            ))

        # Only surface Composio triggers for sources without a bespoke plugin.
        # Bespoke plugins own their source — Composio alternatives are hidden to
        # prevent the LLM from accidentally routing through an external connection.
        if app_name not in registry.bespoke_names:
            from core.integrations.composio_gateway import get_composio_gateway
            gateway = get_composio_gateway()
            if gateway:
                triggers = await gateway.list_trigger_types(app_name)
                from core.integrations.composio_webhooks import derive_source_event
                for t in triggers:
                    slug = t.get("slug") or t.get("name", "")
                    if not slug:
                        continue
                    source, event = derive_source_event(slug)
                    desc = t.get("description") or t.get("display_name", "")
                    results.append(TriggerInfo(source=source, event=event, description=desc, provider="composio"))

        return results

