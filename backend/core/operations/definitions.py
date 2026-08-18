"""Read projection for the Operations "what you've set up" surface."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.activity.service import RUN_STATUSES
from core.operations.models import OperationRunDetail
from core.operations.service import get_trigger_run_detail
from core.scheduling import coerce_datetime_or_none
from core.triggers.models import TriggerRule
from core.triggers.priority import AttentionLevel
from core.triggers.vocabulary import humanize_failure_reason
from services.database.mongodb import mongodb

SetupKind = Literal[
    "automation",
    "schedule",
    "deferred_instruction",
    "reminder",
    "timer",
    "alarm",
    "protocol",
]
SetupStatus = Literal["active", "disabled", "paused"]


class LinkedProtocol(BaseModel):
    name: str
    description: str = ""
    prefetch_safe: bool = False
    step_count: int = 0


class SetupSummary(BaseModel):
    id: str
    source: Literal["trigger_rule", "protocol"]
    kind: SetupKind
    name: str
    series_id: str | None = None
    rule_id: str | None = None
    description: str | None = None
    enabled: bool = True
    status: SetupStatus = "active"
    next_due_at: datetime | None = None
    last_run_at: datetime | None = None
    last_outcome: str | None = None
    latest_instance_id: str | None = None
    decision: str | None = None
    paused_until: datetime | None = None
    origin: dict[str, Any] = Field(default_factory=dict)
    attention: dict[str, Any] = Field(default_factory=dict)
    delivery: dict[str, Any] = Field(default_factory=dict)
    linked_protocol: LinkedProtocol | None = None
    source_label: str = "JARV1S"
    trigger_label: str = "Configured"
    cadence_label: str | None = None
    action_label: str = "Run"


class SetupExplain(BaseModel):
    setup: SetupSummary
    latest_instance_id: str | None = None
    diagnosis: str
    failure_label: str | None = None
    run_detail: OperationRunDetail | None = None


class AutomationDefinitionSummary(BaseModel):
    id: str
    name: str
    enabled: bool
    importance: AttentionLevel
    trigger: dict[str, Any]
    decision: str
    paused_until: datetime | None = None
    created_at: datetime | None = None
    last_run_at: datetime | None = None
    run_count: int = 0
    updated_at: datetime | None = None


def _status_for_rule(rule: TriggerRule, now: datetime) -> SetupStatus:
    if not rule.enabled:
        return "disabled"
    if rule.paused_until and rule.paused_until > now:
        return "paused"
    return "active"


def _kind_for_rule(rule: TriggerRule) -> SetupKind:
    if rule.origin.kind == "external":
        return "automation"
    if rule.action.decision == "act":
        return "deferred_instruction"
    if rule.origin.kind == "interval":
        return "timer"
    if rule.attention.requires_ack or rule.attention.sound == "alarm":
        return "alarm"
    if rule.action.protocol_name:
        return "schedule"
    if rule.action.decision == "tell":
        return "reminder"
    return "schedule"


def _humanize(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").strip().title()


def _source_label_for_rule(rule: TriggerRule) -> str:
    if rule.origin.kind == "external":
        return _humanize(rule.origin.source or "External event")
    return {
        "time": "Schedule",
        "interval": "Timer",
        "manual": "Manual",
        "system": "JARV1S",
    }.get(rule.origin.kind, "JARV1S")


def _trigger_label_for_rule(rule: TriggerRule) -> str:
    if rule.origin.kind == "external":
        source = _humanize(rule.origin.source or "External")
        event = _humanize(rule.origin.event or "event")
        offset = rule.origin.offset_minutes
        if offset:
            direction = "before" if offset < 0 else "after"
            return f"{source} · {event} · {abs(offset)} min {direction}"
        return f"{source} · {event}"
    if rule.origin.kind == "interval" and rule.origin.duration_s:
        minutes = max(1, round(rule.origin.duration_s / 60))
        return f"Every {minutes} min" if rule.origin.recurrence else f"After {minutes} min"
    if rule.origin.original_local_time:
        return f"At {rule.origin.original_local_time}"
    return _humanize(rule.origin.kind)


def _cadence_label_for_rule(rule: TriggerRule) -> str | None:
    if rule.origin.recurrence:
        return _humanize(rule.origin.recurrence)
    if rule.origin.kind == "time":
        return "One time"
    if rule.origin.kind == "interval" and rule.origin.duration_s:
        minutes = max(1, round(rule.origin.duration_s / 60))
        return f"{minutes} min"
    return None


def _action_label_for_rule(rule: TriggerRule) -> str:
    return {
        "tell": "Announce",
        "offer": "Check, then offer",
        "act": "Run quietly",
    }.get(rule.action.decision, _humanize(rule.action.decision))


def _as_dt(value: Any) -> datetime | None:
    return coerce_datetime_or_none(value)


def _latest_timestamp(doc: dict[str, Any]) -> datetime | None:
    return coerce_datetime_or_none(
        doc.get("updated_at")
        or doc.get("acknowledged_at")
        or doc.get("delivered_at")
        or doc.get("completed_at")
        or doc.get("claimed_at")
        or doc.get("due_at")
        or doc.get("created_at")
    )


async def _protocols_by_name(owner_id: str, names: set[str]) -> dict[str, LinkedProtocol]:
    if not names:
        return {}
    cursor = mongodb.db.protocols.find(
        {"owner_id": owner_id, "name": {"$in": list(names)}},
        {
            "_id": 0,
            "id": 1,
            "name": 1,
            "description": 1,
            "steps": 1,
            "prefetch_safe": 1,
        },
    )
    result: dict[str, LinkedProtocol] = {}
    for doc in await cursor.to_list(length=len(names)):
        steps = doc.get("steps") if isinstance(doc.get("steps"), list) else []
        name = str(doc.get("name", ""))
        if name:
            result[name] = LinkedProtocol(
                name=name,
                description=str(doc.get("description", "")),
                prefetch_safe=bool(doc.get("prefetch_safe", False)),
                step_count=len(steps),
            )
    return result


async def _automation_run_stats_by_rule(
    owner_id: str,
    rule_ids: list[str],
) -> dict[str, tuple[datetime | None, int]]:
    if not rule_ids:
        return {}
    cursor = mongodb.db.trigger_instances.find(
        {
            "owner_id": owner_id,
            "source_event.rule_id": {"$in": rule_ids},
            "status": {"$in": list(RUN_STATUSES)},
        },
        {
            "_id": 0,
            "source_event.rule_id": 1,
            "updated_at": 1,
            "acknowledged_at": 1,
            "delivered_at": 1,
            "completed_at": 1,
            "claimed_at": 1,
            "due_at": 1,
            "created_at": 1,
        },
    ).sort("updated_at", -1)

    last_run: dict[str, datetime] = {}
    counts: dict[str, int] = {}
    for doc in await cursor.to_list(length=2000):
        rule_id = str(doc.get("source_event", {}).get("rule_id", ""))
        if rule_id not in rule_ids:
            continue
        counts[rule_id] = counts.get(rule_id, 0) + 1
        if rule_id not in last_run:
            ts = _latest_timestamp(doc)
            if ts is not None:
                last_run[rule_id] = ts
    return {rule_id: (last_run.get(rule_id), counts.get(rule_id, 0)) for rule_id in rule_ids}


async def list_automation_definitions(owner_id: str) -> list[AutomationDefinitionSummary]:
    """List external trigger rules for the Operations definitions surface."""
    cursor = mongodb.db.trigger_rules.find(
        {
            "owner_id": owner_id,
            "origin.kind": "external",
            "surface": True,
        },
        {"_id": 0},
    ).sort("updated_at", -1)
    rules = [TriggerRule.model_validate(doc) for doc in await cursor.to_list(length=200)]
    run_stats = await _automation_run_stats_by_rule(owner_id, [rule.id for rule in rules])

    rows: list[AutomationDefinitionSummary] = []
    for rule in rules:
        last_run_at, run_count = run_stats.get(rule.id, (None, 0))
        rows.append(
            AutomationDefinitionSummary(
                id=rule.id,
                name=rule.name,
                enabled=rule.enabled,
                importance=rule.attention.level,
                trigger={
                    "source": rule.origin.source or "",
                    "event": rule.origin.event or "",
                    "offset": rule.origin.offset_minutes,
                },
                decision=rule.action.decision,
                paused_until=rule.paused_until,
                created_at=rule.created_at,
                last_run_at=last_run_at,
                run_count=run_count,
                updated_at=rule.updated_at,
            )
        )
    return rows


async def _next_due_by_rule(owner_id: str, rule_ids: list[str]) -> dict[str, datetime]:
    if not rule_ids:
        return {}
    cursor = mongodb.db.trigger_instances.find(
        {
            "owner_id": owner_id,
            "rule_id": {"$in": rule_ids},
            "status": {"$in": ["pending", "awaiting_delivery"]},
        },
        {"_id": 0, "rule_id": 1, "due_at": 1},
    ).sort("due_at", 1)
    result: dict[str, datetime] = {}
    for doc in await cursor.to_list(length=1000):
        rule_id = str(doc.get("rule_id", ""))
        due_at = _as_dt(doc.get("due_at"))
        if rule_id and due_at and rule_id not in result:
            result[rule_id] = due_at
    return result


async def _latest_instance_by_rule(owner_id: str, rule_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not rule_ids:
        return {}
    cursor = mongodb.db.trigger_instances.find(
        {
            "owner_id": owner_id,
            "status": {"$in": list(RUN_STATUSES)},
            "$or": [
                {"rule_id": {"$in": rule_ids}},
                {"source_event.rule_id": {"$in": rule_ids}},
            ],
        },
        {
            "_id": 0,
            "id": 1,
            "rule_id": 1,
            "source_event.rule_id": 1,
            "status": 1,
            "updated_at": 1,
            "acknowledged_at": 1,
            "delivered_at": 1,
            "completed_at": 1,
            "claimed_at": 1,
            "due_at": 1,
            "created_at": 1,
            "failure_reason": 1,
        },
    ).sort("updated_at", -1)
    result: dict[str, dict[str, Any]] = {}
    for doc in await cursor.to_list(length=2000):
        rule_id = str(doc.get("rule_id") or doc.get("source_event", {}).get("rule_id") or "")
        if rule_id and rule_id not in result:
            result[rule_id] = doc
    return result


async def _protocol_summaries(owner_id: str) -> list[SetupSummary]:
    cursor = mongodb.db.protocols.find(
        {"owner_id": owner_id},
        {
            "_id": 0,
            "id": 1,
            "name": 1,
            "description": 1,
            "steps": 1,
            "run_count": 1,
            "last_run_at": 1,
            "prefetch_safe": 1,
            "updated_at": 1,
        },
    ).sort("updated_at", -1)
    rows: list[SetupSummary] = []
    for doc in await cursor.to_list(length=200):
        name = str(doc.get("name", "Protocol"))
        steps = doc.get("steps") if isinstance(doc.get("steps"), list) else []
        rows.append(
            SetupSummary(
                id=f"protocol:{doc['id']}",
                source="protocol",
                kind="protocol",
                name=name,
                description=str(doc.get("description", "")),
                enabled=True,
                status="active",
                last_run_at=_as_dt(doc.get("last_run_at")),
                last_outcome="completed" if doc.get("last_run_at") else None,
                linked_protocol=LinkedProtocol(
                    name=name,
                    description=str(doc.get("description", "")),
                    prefetch_safe=bool(doc.get("prefetch_safe", False)),
                    step_count=len(steps),
                ),
                source_label="Saved routine",
                trigger_label="On demand",
                cadence_label=None,
                action_label="Run routine",
            )
        )
    return rows


async def list_setups(
    owner_id: str,
    *,
    kind: SetupKind | None = None,
    status: SetupStatus | None = None,
) -> list[SetupSummary]:
    if kind == "protocol":
        if status is not None and status != "active":
            return []
        return await _protocol_summaries(owner_id)

    query: dict[str, Any] = {"owner_id": owner_id, "surface": True}
    if kind == "automation":
        query["origin.kind"] = "external"
    elif kind in {"schedule", "deferred_instruction", "reminder", "timer", "alarm"}:
        query["origin.kind"] = {"$in": ["time", "interval"]}

    cursor = mongodb.db.trigger_rules.find(query, {"_id": 0}).sort("updated_at", -1)
    docs = await cursor.to_list(length=500)
    rules = [TriggerRule.model_validate(doc) for doc in docs]
    rule_ids = [rule.id for rule in rules]
    next_due = await _next_due_by_rule(owner_id, rule_ids)
    latest = await _latest_instance_by_rule(owner_id, rule_ids)
    protocol_names = {rule.action.protocol_name for rule in rules if rule.action.protocol_name}
    protocols = await _protocols_by_name(owner_id, {str(name) for name in protocol_names})

    now = datetime.now(timezone.utc)
    rows: list[SetupSummary] = []
    for rule in rules:
        derived_kind = _kind_for_rule(rule)
        derived_status = _status_for_rule(rule, now)
        if kind is not None and derived_kind != kind:
            continue
        if status is not None and derived_status != status:
            continue

        latest_doc = latest.get(rule.id)
        rows.append(
            SetupSummary(
                id=f"rule:{rule.id}",
                source="trigger_rule",
                kind=derived_kind,
                name=rule.name,
                series_id=rule.id if rule.origin.kind in {"time", "interval"} else None,
                rule_id=rule.id if rule.origin.kind == "external" else None,
                description=rule.description or rule.action.message or rule.action.instructions,
                enabled=rule.enabled,
                status=derived_status,
                next_due_at=next_due.get(rule.id),
                last_run_at=_latest_timestamp(latest_doc or {}),
                last_outcome=str(latest_doc.get("status")) if latest_doc else None,
                latest_instance_id=str(latest_doc.get("id")) if latest_doc and latest_doc.get("id") else None,
                decision=rule.action.decision,
                paused_until=rule.paused_until,
                origin=rule.origin.model_dump(mode="json", exclude_none=True),
                attention=rule.attention.model_dump(mode="json", exclude_none=True),
                delivery=rule.delivery.model_dump(mode="json", exclude_none=True),
                linked_protocol=protocols.get(rule.action.protocol_name or ""),
                source_label=_source_label_for_rule(rule),
                trigger_label=_trigger_label_for_rule(rule),
                cadence_label=_cadence_label_for_rule(rule),
                action_label=_action_label_for_rule(rule),
            )
        )
    return rows


async def resolve_setup(owner_id: str, name_or_id: str) -> SetupSummary | list[SetupSummary] | None:
    needle = name_or_id.strip()
    if needle.startswith("protocol:"):
        protocol_rows = await _protocol_summaries(owner_id)
        return next((row for row in protocol_rows if row.id == needle), None)

    rows = await list_setups(owner_id)
    if needle.startswith("rule:"):
        return next((row for row in rows if row.id == needle), None)
    bare = needle.removeprefix("rule:")
    exact = [row for row in rows if row.id == f"rule:{bare}" or row.name.casefold() == needle.casefold()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact
    contains = [row for row in rows if needle.casefold() in row.name.casefold()]
    if len(contains) == 1:
        return contains[0]
    if contains:
        return contains
    return None


async def explain_setup(owner_id: str, name_or_id: str) -> SetupExplain | list[SetupSummary] | None:
    resolved = await resolve_setup(owner_id, name_or_id)
    if resolved is None or isinstance(resolved, list):
        return resolved
    if resolved.source == "protocol":
        return SetupExplain(setup=resolved, diagnosis="This is a saved routine; it has no trigger instance history.")
    if resolved.latest_instance_id is None:
        return SetupExplain(
            setup=resolved,
            diagnosis="No trigger instance has been created for this setup yet; check the trigger/origin.",
        )

    detail = await get_trigger_run_detail(owner_id, resolved.latest_instance_id)
    status = detail.status if detail else resolved.last_outcome
    failure_label = humanize_failure_reason(detail.failure_reason if detail else None)
    if status in {"suppressed", "expired", "failed"}:
        diagnosis = "A trigger instance was created but did not complete delivery/action execution."
    else:
        diagnosis = "A trigger instance was created and reached the execution/delivery path."
    return SetupExplain(
        setup=resolved,
        latest_instance_id=resolved.latest_instance_id,
        diagnosis=diagnosis,
        failure_label=failure_label,
        run_detail=detail,
    )
