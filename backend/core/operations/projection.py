"""Provider-composed projection of user-managed configured behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from core.operations.definitions import SetupKind, SetupStatus, SetupSummary, list_setups
from core.scheduling import coerce_datetime_or_none
from core.triggers.lifecycle import is_scheduler_managed_instance
from services.database.mongodb import mongodb

SetupScope = Literal["definition", "occurrence"]
SetupAction = Literal["pause", "resume", "delete"]
SetupType = Literal[
    "schedule",
    "automation",
    "habit_checkin",
    "quiet_window",
    "protocol",
    "scheduled_occurrence",
]


class ManagedSetup(BaseModel):
    """Compact shared view for agent lookup and the Operations UI."""

    resource_ref: str
    resource_id: str
    name: str
    description: str | None = None
    setup_type: SetupType
    managed_by: str
    scope: SetupScope = "definition"
    status: SetupStatus = "active"
    supported_actions: list[SetupAction] = Field(default_factory=list)
    edit_tool: str | None = None
    kind: SetupKind
    series_id: str | None = None
    rule_id: str | None = None
    instance_id: str | None = None
    next_due_at: datetime | None = None
    last_run_at: datetime | None = None
    last_outcome: str | None = None
    paused_until: datetime | None = None
    source_label: str = "JARV1S"
    trigger_label: str = "Configured"
    cadence_label: str | None = None
    action_label: str = "Run"


def _resource_ref(managed_by: str, setup_type: str, resource_id: str) -> str:
    return f"{managed_by}:{setup_type}:{resource_id}"


def _summary_fields(row: SetupSummary) -> dict:
    return {
        "name": row.name,
        "description": row.description,
        "status": row.status,
        "kind": row.kind,
        "series_id": row.series_id,
        "rule_id": row.rule_id,
        "instance_id": row.latest_instance_id,
        "next_due_at": row.next_due_at,
        "last_run_at": row.last_run_at,
        "last_outcome": row.last_outcome,
        "paused_until": row.paused_until,
        "source_label": row.source_label,
        "trigger_label": row.trigger_label,
        "cadence_label": row.cadence_label,
        "action_label": row.action_label,
    }


def _rule_to_managed(row: SetupSummary) -> ManagedSetup:
    managed_by = "automations" if row.kind == "automation" else "scheduler"
    setup_type: SetupType = "automation" if row.kind == "automation" else "schedule"
    resource_id = row.rule_id if managed_by == "automations" else row.series_id
    if resource_id is None:
        raise ValueError(f"{row.name} is missing its managed resource identifier")
    edit_tool = (
        "automations.update_rule"
        if managed_by == "automations"
        else "scheduler.replace_alert"
    )
    return ManagedSetup(
        **_summary_fields(row),
        resource_ref=_resource_ref(managed_by, setup_type, resource_id),
        resource_id=resource_id,
        managed_by=managed_by,
        setup_type=setup_type,
        scope="definition",
        supported_actions=["pause", "resume", "delete"],
        edit_tool=edit_tool,
    )


def _protocol_to_managed(row: SetupSummary) -> ManagedSetup:
    protocol_id = row.id.removeprefix("protocol:")
    return ManagedSetup(
        **_summary_fields(row),
        resource_ref=_resource_ref("protocol", "protocol", protocol_id),
        resource_id=protocol_id,
        managed_by="protocol",
        setup_type="protocol",
        scope="definition",
        supported_actions=["delete"],
        edit_tool="protocol.update_protocol",
    )


async def _habit_checkin_rows(owner_id: str) -> list[ManagedSetup]:
    from plugins.habits.store import get_habit, list_owner_checkin_plans

    rows: list[ManagedSetup] = []
    for plan in await list_owner_checkin_plans(owner_id):
        habit = await get_habit(owner_id, plan.habit_id)
        habit_name = habit.name if habit else plan.habit_id
        is_paused = bool(plan.paused_until and plan.paused_until > datetime.now(timezone.utc))
        status: SetupStatus = "paused" if is_paused else ("active" if plan.active else "disabled")
        rows.append(
            ManagedSetup(
                kind="schedule",
                name=f"{habit_name} check-in",
                description=plan.message,
                status=status,
                managed_by="habits",
                setup_type="habit_checkin",
                resource_ref=_resource_ref("habits", "habit_checkin", plan.id),
                resource_id=plan.id,
                scope="definition",
                supported_actions=["pause", "resume", "delete"],
                edit_tool="habits.replace_habit_checkin",
                trigger_label=plan.when,
                cadence_label=plan.recurrence,
                action_label="Check in",
                source_label="Habit",
                series_id=plan.rule_id,
                paused_until=plan.paused_until,
            )
        )
    return rows


async def _quiet_window_rows(owner_id: str) -> list[ManagedSetup]:
    from core.attention.service import attention_service

    schedules = await attention_service.list_quiet_windows(owner_id)
    rows: list[ManagedSetup] = []
    for window in schedules:
        status: SetupStatus = "active" if window.enabled else "disabled"
        rows.append(
            ManagedSetup(
                kind="schedule",
                name=window.name,
                description=f"{window.start_time}-{window.end_time}",
                status=status,
                managed_by="attention",
                setup_type="quiet_window",
                resource_ref=_resource_ref("attention", "quiet_window", window.id),
                resource_id=window.id,
                scope="definition",
                supported_actions=["delete"],
                edit_tool="attention.set_quiet_window",
                trigger_label=f"{window.start_time}-{window.end_time}",
                cadence_label=", ".join(window.days),
                action_label="Quiet mode",
                source_label="Attention",
            )
        )
    return rows


async def _scheduler_occurrence_rows(owner_id: str) -> list[ManagedSetup]:
    cursor = mongodb.db.trigger_instances.find(
        {
            "owner_id": owner_id,
            "status": {"$in": ["pending", "awaiting_delivery"]},
            "$or": [{"rule_id": None}, {"rule_id": {"$exists": False}}],
        }
    )
    rows: list[ManagedSetup] = []
    for doc in await cursor.to_list(length=200):
        if not is_scheduler_managed_instance(doc):
            continue
        action = doc.get("action_snapshot") or {}
        due_at = coerce_datetime_or_none(doc.get("due_at"))
        label = str(action.get("message") or action.get("instructions") or "Scheduled work")
        rows.append(
            ManagedSetup(
                kind="deferred_instruction" if action.get("decision") == "act" else "reminder",
                name=label[:80],
                description=action.get("instructions") or action.get("message"),
                status="active",
                next_due_at=due_at,
                instance_id=doc["id"],
                managed_by="scheduler",
                setup_type="scheduled_occurrence",
                resource_ref=_resource_ref("scheduler", "scheduled_occurrence", doc["id"]),
                resource_id=doc["id"],
                scope="occurrence",
                supported_actions=["delete"],
                edit_tool="scheduler.replace_alert",
                trigger_label=due_at.isoformat() if due_at else "Pending",
                action_label="Run quietly" if action.get("decision") == "act" else "Announce",
                source_label="Schedule",
            )
        )
    return rows


def _matches_query(row: ManagedSetup, query: str | None) -> bool:
    if not query:
        return True
    needle = query.casefold().strip()
    if not needle:
        return True
    haystack = " ".join(
        str(value or "")
        for value in (
            row.name,
            row.description,
            row.source_label,
            row.trigger_label,
            row.managed_by,
            row.setup_type,
        )
    ).casefold()
    return needle in haystack


async def find_managed_setups(
    owner_id: str,
    *,
    query: str | None = None,
    status: SetupStatus | None = None,
    setup_type: SetupType | None = None,
) -> list[ManagedSetup]:
    rows: list[ManagedSetup] = []
    for summary in await list_setups(owner_id):
        rows.append(_rule_to_managed(summary))
    for summary in await list_setups(owner_id, kind="protocol"):
        rows.append(_protocol_to_managed(summary))
    rows.extend(await _habit_checkin_rows(owner_id))
    rows.extend(await _quiet_window_rows(owner_id))
    rows.extend(await _scheduler_occurrence_rows(owner_id))

    filtered: list[ManagedSetup] = []
    for row in rows:
        if status is not None and row.status != status:
            continue
        if setup_type is not None and row.setup_type != setup_type:
            continue
        if not _matches_query(row, query):
            continue
        filtered.append(row)
    return filtered


async def resolve_managed_setup(
    owner_id: str,
    target: str,
) -> ManagedSetup | list[ManagedSetup] | None:
    needle = target.strip()
    if not needle:
        return None
    all_rows = await find_managed_setups(owner_id)
    exact = [
        row
        for row in all_rows
        if needle in {
            row.resource_ref,
            row.resource_id,
            row.series_id,
            row.rule_id,
            row.instance_id,
        }
        or row.name.casefold() == needle.casefold()
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return exact

    rows = [row for row in all_rows if _matches_query(row, needle)]
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        return rows
    return None
