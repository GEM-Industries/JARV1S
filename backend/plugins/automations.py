"""
Automations Plugin — LLM-facing tools for creating and managing automation rules.

Rules are stored as external-origin TriggerRule records and evaluated by the
AutomationService background daemon, which creates TriggerInstance documents
when rule conditions are met.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.context import get_owner_id, get_tz
from core.decorators import tool
from core.operations.events import publish_operations_changed
from core.operations.projection import resolve_managed_setup
from core.plugins.mutations import merge_model_patch, validation_error_message
from core.plugins.result import ToolResult
from core.plugins.ui import receipt_envelope
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.time import parse_duration
from core.triggers.conditions import (
    field_condition_dicts,
    field_conditions_from_dicts,
)
from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    ManagementOwnership,
    TriggerAction,
    TriggerOrigin,
    TriggerRule,
    validate_trigger_action_fields,
)
from core.triggers.lifecycle import cancel_open_instances_for_rule
from core.triggers.priority import AttentionLevel, attention_policy_fields
from core.triggers.service import trigger_service
from core.triggers.vocabulary import DECISION_TELL, TriggerDecision
from core.plugins.capabilities import CapabilityErrorDetail
from services.database.mongodb import mongodb


_INTERNAL_SOURCES = {"scheduler", "time", "interval", "cron", "system", "manual"}
_IDENTITY_FIELDS = frozenset({"user", "sender", "from", "author", "user_id", "sender_id"})
_SCHEMA_META = {
    "type",
    "title",
    "description",
    "$schema",
    "additionalProperties",
    "required",
    "items",
}
_OPS_BY_TYPE: dict[str, tuple[str, ...]] = {
    "string": ("contains", "not_contains", "equals", "not_equals"),
    "number": ("equals", "not_equals", "greater_than", "less_than"),
    "boolean": ("equals", "not_equals"),
}


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


def _freshness_policy(trigger: dict[str, Any]) -> FreshnessPolicy:
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


async def _resolve_slug(source: str, event: str) -> tuple[str | None, list[str]]:
    """Resolve a derived (source, event) pair back to the Composio trigger slug."""
    from core.integrations.composio_gateway import get_composio_gateway

    gateway = get_composio_gateway()
    if not gateway:
        return None, []
    triggers = await gateway.list_trigger_types(source)
    if not triggers:
        return None, []
    from core.integrations.composio_webhooks import derive_source_event

    valid_events = []
    matched = None
    for item in triggers:
        slug = item.get("slug") or item.get("name", "")
        if not slug:
            continue
        derived_source, derived_event = derive_source_event(slug)
        valid_events.append(derived_event)
        if (derived_source, derived_event) == (source, event):
            matched = slug
    return matched, valid_events


async def _deregister_push_trigger(source: str, event: str) -> None:
    slug, _ = await _resolve_slug(source, event)
    if not slug:
        return
    from core.integrations.composio_gateway import get_composio_gateway

    gateway = get_composio_gateway()
    if gateway:
        await gateway.deregister_trigger(source, slug)


async def _source_event_in_use(
    owner_id: str,
    source: str,
    event: str,
    *,
    exclude_rule_id: str | None = None,
) -> bool:
    query: dict[str, Any] = {
        "owner_id": owner_id,
        "origin.kind": "external",
        "origin.source": source,
        "origin.event": event,
    }
    if exclude_rule_id:
        query["id"] = {"$ne": exclude_rule_id}
    return await mongodb.db.trigger_rules.find_one(query, {"_id": 1}) is not None


async def _deregister_if_unused(
    owner_id: str,
    source: str,
    event: str,
    *,
    exclude_rule_id: str | None = None,
) -> None:
    if not source or not event:
        return
    if await _source_event_in_use(
        owner_id, source, event, exclude_rule_id=exclude_rule_id
    ):
        return
    await _deregister_push_trigger(source, event)


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
    await _deregister_if_unused(owner_id, source, event)
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


def _interval_trigger_error(source: str, event: str) -> CapabilityErrorDetail | None:
    if source in _INTERNAL_SOURCES or event == "interval":
        return _fail(
            "create_rule is for external event sources, not time intervals. "
            "Use scheduler.remind(when='45m', message='...', recurrence='every 45m', "
            "instructions='only if ...') for recurring reminders."
        )
    return None


def _provided(**fields: Any) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value is not None}


def _condition_key(condition: dict[str, Any]) -> tuple[str, str, str]:
    return (condition["field"], condition["op"], condition["value"])


def _validate_condition_list(raw: list[dict[str, Any]]) -> list[dict[str, Any]] | CapabilityErrorDetail:
    try:
        return [ConditionConfig.model_validate(c).model_dump() for c in raw]
    except ValidationError as error:
        return validation_error_message("condition", ConditionConfig, error)


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


def _event_hint(event: str, catalog: list[_CatalogTrigger]) -> str:
    valid_events = [row.info.event for row in catalog]
    if event.startswith("event_"):
        stripped = event.removeprefix("event_")
        if stripped in valid_events:
            return f" Use event='{stripped}'."
    tokens = {part for part in event.lower().replace("-", "_").split("_") if part}
    related = [
        row for row in catalog
        if tokens & set(row.info.event.lower().split("_"))
    ][:2]
    if not related:
        return ""
    parts: list[str] = []
    for row in related:
        fields = [item.field for item in row.info.condition_fields]
        extra = f" fields={fields}" if fields else ""
        parts.append(f"{row.info.event}{extra}")
    return f" Related: {', '.join(parts)}."


def _rewrite_identity_fields(
    catalog: _CatalogTrigger,
    conditions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    advertised = {item.field for item in catalog.info.condition_fields}
    identity = advertised & _IDENTITY_FIELDS
    rewritten: list[dict[str, Any]] = []
    for condition in conditions:
        field = str(condition["field"])
        if field in advertised:
            rewritten.append(condition)
            continue
        if field in _IDENTITY_FIELDS and len(identity) == 1:
            rewritten.append({**condition, "field": next(iter(identity))})
            continue
        rewritten.append(condition)
    return rewritten


def _schema_properties(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        return {}
    nested = schema.get("properties")
    if isinstance(nested, dict):
        return nested
    if all(isinstance(item, dict) for item in schema.values()) and not set(schema) <= _SCHEMA_META:
        return {key: item for key, item in schema.items() if key not in _SCHEMA_META}
    return {}


def _requires_provider_config(config: Any) -> bool:
    if not isinstance(config, dict) or not config:
        return False
    required = config.get("required")
    if isinstance(required, list) and any(str(item).strip() for item in required):
        return True
    return bool(_schema_properties(config))


def _json_field_type(spec: Any) -> str | None:
    if not isinstance(spec, dict):
        return "string"
    declared = spec.get("type")
    if declared == "object" or (isinstance(spec.get("properties"), dict) and declared is None):
        return None
    if declared in {"integer", "number"}:
        return "number"
    if declared == "boolean":
        return "boolean"
    return "string"


def _condition_field(field: str, field_type: str, hint: str | None = None) -> ConditionFieldInfo:
    return ConditionFieldInfo(
        field=field,
        type=field_type,
        hint=hint,
        operators=list(_OPS_BY_TYPE.get(field_type, _OPS_BY_TYPE["string"])),
    )


def _condition_fields_from_watcher(raw: list[dict[str, Any]]) -> list[ConditionFieldInfo]:
    fields: list[ConditionFieldInfo] = []
    for item in raw:
        name = item.get("field")
        if not name:
            continue
        declared = str(item.get("type") or "string")
        field_type = declared if declared in _OPS_BY_TYPE else "string"
        hint = item.get("hint")
        fields.append(_condition_field(str(name), field_type, str(hint) if hint else None))
    return fields


def _condition_fields_from_payload(payload: Any) -> list[ConditionFieldInfo]:
    fields: list[ConditionFieldInfo] = []
    for name, spec in _schema_properties(payload).items():
        field_type = _json_field_type(spec)
        if not field_type:
            continue
        hint = spec.get("description") if isinstance(spec, dict) else None
        fields.append(_condition_field(str(name), field_type, str(hint) if hint else None))
    return fields


def _builtin_trigger_rows(source: str) -> list[_CatalogTrigger]:
    from services.automation import automation_service

    events = automation_service.watcher_trigger_info(source)
    if not events:
        return []
    fields = _condition_fields_from_watcher(
        automation_service.watcher_condition_fields(source)
    )
    reactive = automation_service.watcher_is_reactive(source)
    rows: list[_CatalogTrigger] = []
    for item in events:
        rows.append(
            _CatalogTrigger(
                info=TriggerInfo(
                    source=source,
                    event=item.get("event", ""),
                    description=item.get("description", ""),
                    provider="built-in",
                    condition_fields=fields,
                    supported=True,
                    offset_supported=not reactive,
                )
            )
        )
    return rows


async def _composio_trigger_rows(source: str) -> list[_CatalogTrigger]:
    from core.integrations.composio_gateway import get_composio_gateway
    from core.integrations.composio_webhooks import derive_source_event

    gateway = get_composio_gateway()
    if gateway is None:
        return []
    rows: list[_CatalogTrigger] = []
    for item in await gateway.list_trigger_types(source):
        slug = item.get("slug") or item.get("name", "")
        if not slug:
            continue
        derived_source, event = derive_source_event(slug)
        requires_config = _requires_provider_config(item.get("config"))
        description = item.get("description") or item.get("display_name") or ""
        if requires_config:
            description = (
                f"{description} Requires provider configuration that automations "
                "cannot store yet — do not use this event."
            ).strip()
        rows.append(
            _CatalogTrigger(
                info=TriggerInfo(
                    source=derived_source or source,
                    event=event,
                    description=description,
                    provider="composio",
                    condition_fields=_condition_fields_from_payload(item.get("payload")),
                    supported=not requires_config,
                    offset_supported=False,
                ),
                slug=slug,
            )
        )
    return rows


async def _catalog_for_source(source: str) -> list[_CatalogTrigger] | CapabilityErrorDetail:
    if not source:
        return _fail("trigger.source is required. Call list_available_triggers(source) first.")
    if source in _INTERNAL_SOURCES:
        return _fail(
            f"source='{source}' is not an external event source. "
            "Use scheduler tools for time-based rules."
        )

    from core.plugins.registry import registry

    rows = _builtin_trigger_rows(source)
    if source not in registry.bespoke_names:
        composio_rows = await _composio_trigger_rows(source)
        if not rows and not composio_rows:
            from core.integrations.composio_gateway import get_composio_gateway

            if get_composio_gateway() is None:
                return _fail(
                    f"No built-in triggers for '{source}' and Composio is not configured. "
                    f"Connect the app, then call list_available_triggers('{source}')."
                )
            return _fail(
                f"No triggers available for '{source}'. "
                f"Call list_available_triggers('{source}') after connecting the app."
            )
        rows.extend(composio_rows)
    if not rows:
        return _fail(
            f"No triggers available for '{source}'. "
            f"Call list_available_triggers('{source}') for valid source/event values."
        )
    return rows


async def _resolve_catalog_trigger(
    source: str,
    event: str,
) -> _CatalogTrigger | CapabilityErrorDetail:
    catalog = await _catalog_for_source(source)
    if isinstance(catalog, CapabilityErrorDetail):
        return catalog
    matches = [row for row in catalog if row.info.event == event]
    if not matches:
        valid_events = [row.info.event for row in catalog]
        return _fail(
            f"event='{event}' is not valid for source='{source}'. "
            f"Valid events are: {valid_events}."
            f"{_event_hint(event, catalog)} "
            f"Call list_available_triggers('{source}') and use an exact event value."
        )
    chosen = matches[0]
    if not chosen.info.supported:
        return _fail(
            f"event='{event}' for source='{source}' requires provider configuration "
            "that automations cannot store yet. Use a supported event from "
            f"list_available_triggers('{source}')."
        )
    return chosen


def _condition_error_for_catalog(
    catalog: _CatalogTrigger,
    conditions: list[dict[str, Any]],
) -> CapabilityErrorDetail | None:
    if not conditions:
        return None
    advertised = {field.field: field for field in catalog.info.condition_fields}
    if not advertised:
        return _fail(
            f"source='{catalog.info.source}' event='{catalog.info.event}' has no "
            "filterable payload fields. Leave field/value empty and put fire-time "
            "policy in instructions."
        )
    invalid_fields = sorted({
        str(condition.get("field")) for condition in conditions
    } - set(advertised))
    if invalid_fields:
        return _fail(
            f"Invalid condition field(s) for source='{catalog.info.source}': "
            f"{invalid_fields}. Use one of: {sorted(advertised)}. "
            f"Copy field from list_available_triggers('{catalog.info.source}'). "
            "If the policy cannot be expressed with those fields, put it in instructions."
        )
    for condition in conditions:
        field = advertised[str(condition["field"])]
        op = str(condition["op"])
        if op not in field.operators:
            return _fail(
                f"op='{op}' is not valid for field='{field.field}' ({field.type}). "
                f"Use one of: {list(field.operators)}."
            )
    return None


def _offset_error(catalog: _CatalogTrigger, offset: int) -> CapabilityErrorDetail | None:
    if offset and not catalog.info.offset_supported:
        return _fail(
            f"trigger.offset is only valid for anticipated events. "
            f"{catalog.info.source}.{catalog.info.event} is reactive — use offset=0."
        )
    return None


async def _ensure_push_registered(catalog: _CatalogTrigger) -> CapabilityErrorDetail | None:
    if catalog.info.provider != "composio" or not catalog.slug:
        return None
    from core.integrations.composio_gateway import get_composio_gateway

    gateway = get_composio_gateway()
    if gateway is None:
        return _fail(
            f"source='{catalog.info.source}' requires a connected Composio integration. "
            f"Connect it first, then call list_available_triggers('{catalog.info.source}')."
        )
    ok = await gateway.register_trigger(catalog.info.source, catalog.slug)
    if not ok:
        return _fail(
            f"Could not register push trigger for '{catalog.info.source}.{catalog.info.event}'. "
            f"Is '{catalog.info.source}' connected?"
        )
    return None


async def _duplicate_name_error(owner_id: str, name: str) -> CapabilityErrorDetail | None:
    existing = await mongodb.db.trigger_rules.find_one(
        {
            "owner_id": owner_id,
            "name": re.compile(f"^{re.escape(name.strip())}$", re.IGNORECASE),
            "surface": True,
            "origin.kind": "external",
        },
        {"_id": 0, "id": 1, "name": 1},
    )
    if not existing:
        return None
    return _fail(
        f"Automation {existing.get('name')!r} already exists "
        f"(rule_id={existing['id']!r}). Update it with automations.update_rule; "
        "do not create a duplicate."
    )


def _action_contract_error(action: dict[str, Any]) -> CapabilityErrorDetail | None:
    try:
        validate_trigger_action_fields(
            decision=action.get("decision", DECISION_TELL),
            message=action.get("message") or "",
            instructions=action.get("instructions"),
            protocol_name=action.get("protocol"),
        )
    except ValueError as exc:
        return _fail(str(exc))
    return None


async def _protocol_error(action: dict[str, Any], owner_id: str) -> CapabilityErrorDetail | None:
    protocol_name = action.get("protocol")
    if not protocol_name:
        return None
    from plugins.protocol import protocol_exists

    if await protocol_exists(protocol_name, owner_id):
        return None
    return _fail(
        f"Protocol '{protocol_name}' not found. Use setups.find(setup_type='protocol') "
        "to choose an existing protocol, or set instructions for one-off live work."
    )


def _matches_query(info: TriggerInfo, query: str | None) -> bool:
    needle = (query or "").strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        [
            info.source,
            info.event,
            info.description,
            info.provider,
            *[field.field for field in info.condition_fields],
        ]
    ).lower()
    return needle in haystack or all(part in haystack for part in needle.split())


ConditionOp = Literal[
    "contains", "not_contains", "equals", "not_equals", "greater_than", "less_than"
]


class TriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    event: str = ""
    offset: int = 0


class ConditionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    op: ConditionOp
    value: str


class ActionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: TriggerDecision = DECISION_TELL
    message: str = "Automation triggered."
    protocol: Optional[str] = None
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


class ConditionFieldInfo(BaseModel):
    field: str
    type: str = "string"
    hint: Optional[str] = None
    operators: list[str] = Field(default_factory=list)


class TriggerInfo(BaseModel):
    source: str
    event: str
    description: str
    provider: str
    condition_fields: list[ConditionFieldInfo] = Field(default_factory=list)
    supported: bool = True
    offset_supported: bool = False


@dataclass
class _CatalogTrigger:
    info: TriggerInfo
    slug: str | None = None


class AutomationsPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="automations",
        version="1.0.0",
        description=(
            "Create event-based rules for external apps. Use for 'tell me when...' "
            "and 'before each meeting' requests; use scheduler for clock times."
        ),
        utterances=[
            "set up an automation",
            "let me know when something happens",
            "tell me when something happens",
            "watch for something and tell me",
            "when I get an email, let me know",
            "tell me when someone messages me on Slack",
            "alert me when I get a GitHub pull request",
            "ten minutes before each appointment",
            "remind me before meetings",
            "heads-up before my calendar events",
            "delete that automation",
            "pause my automations",
            "turn my automations back on",
            "what can you watch for",
        ],
    )

    @tool
    async def create_rule(
        self,
        name: str,
        source: str,
        event: str = "",
        offset: int = 0,
        decision: TriggerDecision = DECISION_TELL,
        message: str = "Automation triggered.",
        protocol: Optional[str] = None,
        instructions: Optional[str] = None,
        field: Optional[str] = None,
        op: ConditionOp = "equals",
        value: Optional[str] = None,
        importance: AttentionLevel = "normal",
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Create an event-based automation and persist it. Use for notify/alert/remind-me-when requests tied to external app events; use scheduler tools for clock times.
        Copy exact source and event from list_available_triggers or a validation error.
        field/op/value is an optional catalog filter. instructions are fire-time policy, not a substitute for a filterable field.
        offset is minutes relative to anticipated events only (negative = before); reactive events require 0.
        decision: tell announces; offer evaluates at fire time; act does headless work and requires instructions or protocol.
        Delete a named automation with delete_rule. Pause or resume from inventory with setups.pause / setups.resume.
        """
        interval_error = _interval_trigger_error(source, event)
        if interval_error:
            return interval_error

        trigger_dict = TriggerConfig(source=source, event=event, offset=offset).model_dump()
        action_dict = ActionConfig(
            decision=decision,
            message=message,
            protocol=protocol,
            instructions=instructions,
        ).model_dump()

        catalog = await _resolve_catalog_trigger(
            trigger_dict.get("source", ""),
            trigger_dict.get("event", ""),
        )
        if isinstance(catalog, CapabilityErrorDetail):
            return catalog
        offset_error = _offset_error(catalog, int(trigger_dict.get("offset", 0) or 0))
        if offset_error:
            return offset_error

        action_error = _action_contract_error(action_dict)
        if action_error:
            return action_error

        owner_id = get_owner_id()
        protocol_error = await _protocol_error(action_dict, owner_id)
        if protocol_error:
            return protocol_error
        duplicate_error = await _duplicate_name_error(owner_id, name)
        if duplicate_error:
            return duplicate_error

        validated_conditions: list[dict[str, Any]] = []
        if field is not None or value is not None:
            if not field or value is None:
                return _fail("field and value must be provided together for a filter.")
            collected = _validate_condition_list(
                [{"field": field, "op": op, "value": value}]
            )
            if isinstance(collected, CapabilityErrorDetail):
                return collected
            validated_conditions = _rewrite_identity_fields(catalog, collected)
        condition_error = _condition_error_for_catalog(catalog, validated_conditions)
        if condition_error:
            return condition_error

        register_error = await _ensure_push_registered(catalog)
        if register_error:
            return register_error

        rule = await trigger_service.create_rule(
            owner_id=owner_id,
            name=name,
            origin=_trigger_origin(trigger_dict),
            conditions=field_conditions_from_dicts(validated_conditions),
            action=_trigger_action(action_dict),
            attention=_attention_policy(importance),
            delivery=_delivery_plan(),
            freshness=_freshness_policy(trigger_dict),
            management=ManagementOwnership(provider="automations", resource_id=None),
        )
        await publish_operations_changed(owner_id, "automations")

        msg = f"Automation '{name}' created (id: {rule.id})."
        if catalog.info.provider == "composio":
            msg += " Push trigger registered — fires when the provider delivers a webhook."
        else:
            msg += " Use test_rule to verify against current data."
        disable_path = (
            f"Delete with delete_rule({name!r}). Pause with setups.pause({name!r})."
        )
        trigger_label = f"{trigger_dict.get('source', '')}.{trigger_dict.get('event', '')}"
        action_label = action_dict.get("message") or action_dict.get("protocol") or "Automation"
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
        source: Optional[str] = None,
        event: Optional[str] = None,
        offset: Optional[int] = None,
        conditions: Optional[list[ConditionConfig]] = None,
        add_conditions: Optional[list[ConditionConfig]] = None,
        remove_conditions: Optional[list[ConditionConfig]] = None,
        decision: Optional[TriggerDecision] = None,
        message: Optional[str] = None,
        protocol: Optional[str] = None,
        instructions: Optional[str] = None,
        importance: Optional[AttentionLevel] = None,
    ) -> str | CapabilityErrorDetail:
        """
        Modify an existing event automation. Only provided fields are updated.
        For small filter tweaks use add_conditions/remove_conditions; use conditions=[] to clear all.
        Conditions may only use condition_fields from list_available_triggers; do not invent fields.
        Use instructions for fire-time policy when structured fields cannot express it; pass instructions="" to clear.
        Permanently remove with delete_rule. Pause or resume from inventory with setups.pause / setups.resume.
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

        trigger_patch = _provided(source=source, event=event, offset=offset)
        action_patch = _provided(
            decision=decision,
            message=message,
            protocol=protocol,
            instructions=instructions,
        )
        condition_replace = None if conditions is None else [
            item.model_dump() if isinstance(item, ConditionConfig) else dict(item)
            for item in conditions
        ]
        add_dicts = None if add_conditions is None else [
            item.model_dump() if isinstance(item, ConditionConfig) else dict(item)
            for item in add_conditions
        ]
        remove_dicts = None if remove_conditions is None else [
            item.model_dump() if isinstance(item, ConditionConfig) else dict(item)
            for item in remove_conditions
        ]

        existing_rule = await mongodb.db.trigger_rules.find_one(
            {"id": rule_id, "owner_id": owner_id, "origin.kind": "external"},
        )
        if not existing_rule:
            return _fail(f"No rule found with id '{rule_id}'.")

        rule_model = TriggerRule.model_validate(existing_rule)
        existing = _automation_rule_model(rule_model).model_dump()
        previous_trigger = dict(existing.get("trigger") or {})
        merged_trigger = previous_trigger

        if trigger_patch:
            try:
                merged_trigger = merge_model_patch(
                    TriggerConfig, existing.get("trigger"), trigger_patch
                )
            except ValidationError as error:
                return validation_error_message("trigger", TriggerConfig, error)

        catalog = await _resolve_catalog_trigger(
            merged_trigger.get("source", ""),
            merged_trigger.get("event", ""),
        )
        if isinstance(catalog, CapabilityErrorDetail):
            return catalog
        offset_error = _offset_error(catalog, int(merged_trigger.get("offset", 0) or 0))
        if offset_error:
            return offset_error
        if trigger_patch:
            updates["origin"] = _trigger_origin(merged_trigger).model_dump(mode="python")
            updates["freshness"] = _freshness_policy(merged_trigger).model_dump(mode="python")

        if condition_replace is not None:
            validated_conditions = _validate_condition_list(condition_replace)
            if isinstance(validated_conditions, CapabilityErrorDetail):
                return validated_conditions
            validated_conditions = _rewrite_identity_fields(catalog, validated_conditions)
            condition_error = _condition_error_for_catalog(catalog, validated_conditions)
            if condition_error:
                return condition_error
            updates["conditions"] = [
                condition.model_dump(mode="python")
                for condition in field_conditions_from_dicts(validated_conditions)
            ]
        elif add_dicts is not None or remove_dicts is not None:
            if add_dicts:
                validated_add = _validate_condition_list(add_dicts)
                if isinstance(validated_add, CapabilityErrorDetail):
                    return validated_add
                add_dicts = _rewrite_identity_fields(catalog, validated_add)
                condition_error = _condition_error_for_catalog(catalog, add_dicts)
                if condition_error:
                    return condition_error
            patched = _apply_condition_patches(
                existing.get("conditions", []),
                add=add_dicts,
                remove=remove_dicts,
            )
            if isinstance(patched, CapabilityErrorDetail):
                return patched
            condition_error = _condition_error_for_catalog(catalog, patched)
            if condition_error:
                return condition_error
            updates["conditions"] = [
                condition.model_dump(mode="python")
                for condition in field_conditions_from_dicts(patched)
            ]

        if action_patch:
            try:
                action_update = merge_model_patch(
                    ActionConfig, existing.get("action"), action_patch
                )
            except ValidationError as error:
                return validation_error_message("action", ActionConfig, error)
            action_error = _action_contract_error(action_update)
            if action_error:
                return action_error
            protocol_error = await _protocol_error(action_update, owner_id)
            if protocol_error:
                return protocol_error
            updates["action"] = _trigger_action(action_update).model_dump(mode="python")
            updates["attention"] = _attention_policy(
                importance if importance is not None else existing.get("importance", "normal"),
            ).model_dump(mode="python")
            updates["delivery"] = _delivery_plan().model_dump(mode="python")
        elif importance is not None:
            updates["attention"] = _attention_policy(importance).model_dump(mode="python")

        trigger_changed = (
            previous_trigger.get("source") != merged_trigger.get("source")
            or previous_trigger.get("event") != merged_trigger.get("event")
        )
        if trigger_changed:
            register_error = await _ensure_push_registered(catalog)
            if register_error:
                return register_error

        result = await mongodb.db.trigger_rules.update_one(
            {"id": rule_id, "owner_id": owner_id, "origin.kind": "external"},
            {"$set": updates},
        )
        if result.matched_count == 0:
            return _fail(f"No rule found with id '{rule_id}'.")
        if trigger_changed:
            await _deregister_if_unused(
                owner_id,
                previous_trigger.get("source", ""),
                previous_trigger.get("event", ""),
                exclude_rule_id=rule_id,
            )
        await publish_operations_changed(owner_id, "automations")
        return f"Rule '{rule_id}' updated."

    @tool
    async def delete_rule(self, rule_id: str) -> str | CapabilityErrorDetail:
        """
        Permanently delete one event automation by id or unique name.
        Use for 'delete that automation' after create_rule. Time-based reminders use scheduler.cancel_alert.
        """
        owner_id = get_owner_id()
        resolved = await _resolve_automation_rule_id(owner_id, rule_id)
        if isinstance(resolved, CapabilityErrorDetail):
            return resolved
        deleted = await delete_automation_rule(owner_id, resolved)
        if deleted is None:
            return _fail(f"No automation matching {rule_id!r}.")
        return f"Deleted automation {deleted.name}."

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
        Polling sources: shows upcoming matches with fire times. Push-triggered rules: no live dry-run.
        VOICE: "That rule would fire for your 10am Standup today."
        """
        from services.automation import automation_service

        owner_id = get_owner_id()
        resolved = await _resolve_automation_rule_id(owner_id, rule_id)
        if isinstance(resolved, CapabilityErrorDetail):
            return resolved
        rule_id = resolved
        results = await automation_service.test_rule(rule_id, owner_id=owner_id)
        hold = automation_service.pause_observation()

        if results is None:
            rule = await mongodb.db.trigger_rules.find_one({
                "id": rule_id,
                "owner_id": owner_id,
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
                "No live dry-run is available. The provider trigger is configured, "
                "but JARV1S cannot verify delivery until an event arrives."
            )
            return f"{hold}\n{body}" if hold else body

        if not results:
            body = "No events match this rule in the current 24-hour window."
            return f"{hold}\n{body}" if hold else body

        tz = get_tz()
        now_local = datetime.now(timezone.utc).astimezone(tz)
        today = now_local.date()

        lines = [f"This rule would fire for {len(results)} event(s):"]
        for row in results:
            fire_utc = datetime.fromisoformat(row["fire_time"])
            fire_local = fire_utc.astimezone(tz)
            fire_date = fire_local.date()

            if fire_date == today:
                day = "today"
            elif fire_date == today + timedelta(days=1):
                day = "tomorrow"
            else:
                day = fire_local.strftime("%A %-d %b")

            time_str = fire_local.strftime("%I:%M %p").lstrip("0")

            seconds = row["seconds_until_fire"]
            if seconds > 0:
                mins = int(seconds // 60)
                relative = f"in {mins} min" if mins < 60 else f"in {mins // 60}h {mins % 60}m"
            else:
                relative = "fire time has passed"

            fired = " — already fired this cycle" if row.get("already_fired") else ""
            lines.append(f"  - {row['title']} {day} at {time_str} ({relative}){fired}")

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
    async def list_available_triggers(
        self,
        source: str,
        query: str | None = None,
    ) -> list[TriggerInfo] | CapabilityErrorDetail:
        """
        List trigger types and condition fields available to author a new rule for a source.
        This is not an inventory of existing automations; use setups.find for configured rules.
        Copy the exact source and event into create_rule. Prefer provider="built-in" when present.
        supported=false means the event requires provider configuration automations cannot store yet.
        query optionally filters the returned rows without truncating matches.
        """
        catalog = await _catalog_for_source(source)
        if isinstance(catalog, CapabilityErrorDetail):
            return catalog
        return [row.info for row in catalog if _matches_query(row.info, query)]
