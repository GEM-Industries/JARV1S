"""Recent owner activity: merge domain projectors into one timeline."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from core.activity.headless import fetch_headless_activity_items
from core.activity.models import ActivityItem, ActivityKind, ActivityOutcome
from core.config import settings
from core.time import local_datetime_fields
from core.triggers.projection import trigger_run_kind, trigger_run_source
from core.triggers.vocabulary import humanize_failure_reason
from services.database.mongodb import mongodb

DEFAULT_ACTIVITY_LIMIT = 50
MAX_ACTIVITY_LIMIT = 200
SUMMARY_CHARS = 180
RUN_STATUSES = (
    "claimed",
    "executing",
    "completed",
    "delivered",
    "acknowledged",
    "awaiting_delivery",
    "suppressed",
    "expired",
    "failed",
)


def _time_fields_from_dt(value: datetime | None) -> tuple[str, str]:
    if value is None:
        value = datetime.now(timezone.utc)
    fields = local_datetime_fields(value)
    return fields["time"], fields["utc_time"]


def _normalize_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("Activity limit must be at least 1")
    if limit > MAX_ACTIVITY_LIMIT:
        raise ValueError(f"Activity limit must be <= {MAX_ACTIVITY_LIMIT}")
    return limit


async def _recent_headless_turns(owner_id: str, limit: int) -> list[ActivityItem]:
    return await fetch_headless_activity_items(owner_id=owner_id, limit=limit)


async def _recent_user_turns(owner_id: str, limit: int) -> list[ActivityItem]:
    # Lazy import: core.operations.service imports core.activity.models only,
    # but keep this local to sidestep any module-load ordering surprises.
    from core.operations.service import list_user_turns

    return await list_user_turns(owner_id, limit=limit)


async def _recent_tasks(owner_id: str, limit: int) -> list[ActivityItem]:
    col = mongodb.get_collection("background_tasks")
    cursor = col.find(
        {"owner_id": owner_id},
        {"events": 0, "_id": 0},
    ).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    items: list[ActivityItem] = []
    for doc in docs:
        status = doc.get("status", "running")
        if status == "running":
            outcome: ActivityOutcome = "running"
        elif status == "failed":
            outcome = "failed"
        else:
            outcome = "completed"
        summary = (doc.get("progress_summary") or doc.get("prompt") or "Background task")[:SUMMARY_CHARS]
        ts = doc.get("completed_at") or doc.get("created_at")
        when, sort_at = _time_fields_from_dt(ts)
        items.append(
            ActivityItem(
                kind="task",
                id=str(doc["task_id"]),
                summary=summary,
                when=when,
                sort_at=sort_at,
                outcome=outcome,
                delivery=None,
                source=(doc.get("source") or doc.get("mode")),
            )
        )
    return items


def _run_outcome(status: str) -> ActivityOutcome:
    if status == "failed" or status == "expired":
        return "failed"
    if status == "claimed" or status == "executing":
        return "running"
    if status == "awaiting_delivery":
        return "awaiting"
    return "completed"


def _run_timestamp(doc: dict) -> datetime | None:
    return (
        doc.get("updated_at")
        or doc.get("acknowledged_at")
        or doc.get("delivered_at")
        or doc.get("completed_at")
        or doc.get("claimed_at")
        or doc.get("due_at")
        or doc.get("created_at")
    )


def _run_source(source_event: dict, action: dict, origin: dict) -> str | None:
    return trigger_run_source(
        {
            "source_event": source_event,
            "action_snapshot": action,
            "origin_snapshot": origin,
        }
    )


def _run_kind(source_event: dict, origin: dict) -> ActivityKind:
    return trigger_run_kind(
        {"source_event": source_event, "origin_snapshot": origin}
    )


def _run_summary(kind: ActivityKind, outcome: ActivityOutcome, doc: dict, action: dict, source: str | None) -> str:
    result = doc.get("result_text")
    if result:
        return str(result)[:SUMMARY_CHARS]

    message = str(action.get("message") or "")
    label = "Automation" if kind == "automation" else "Trigger"

    if message:
        return message[:SUMMARY_CHARS]
    if outcome == "awaiting":
        return f"{label} awaiting delivery"
    if source:
        return f"{label} {source!r} {doc.get('status', 'completed')}"[:SUMMARY_CHARS]
    return f"{label} {doc.get('status', 'completed')}"[:SUMMARY_CHARS]


async def _recent_runs(owner_id: str, limit: int, *, kind: ActivityKind | None = None) -> list[ActivityItem]:
    """Recent trigger-instance runs, including automation fires."""
    query: dict = {"owner_id": owner_id, "status": {"$in": list(RUN_STATUSES)}}
    if kind == "automation":
        query.update({"origin_snapshot.kind": "external", "source_event.rule_id": {"$exists": True}})
    elif kind == "trigger":
        query["$or"] = [
            {"origin_snapshot.kind": {"$ne": "external"}},
            {"source_event.rule_id": {"$exists": False}},
        ]

    cursor = mongodb.db.trigger_instances.find(
        query,
    ).sort("updated_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)

    items: list[ActivityItem] = []
    for doc in docs:
        source_event = doc.get("source_event") or {}
        action = doc.get("action_snapshot") or {}
        origin = doc.get("origin_snapshot") or {}
        if not isinstance(source_event, dict):
            source_event = {}
        if not isinstance(action, dict):
            action = {}
        if not isinstance(origin, dict):
            origin = {}

        run_kind = _run_kind(source_event, origin)
        if kind is not None and run_kind != kind:
            continue

        outcome = _run_outcome(str(doc.get("status", "")))
        source = _run_source(source_event, action, origin)
        summary = _run_summary(run_kind, outcome, doc, action, source)
        failure_label = (
            humanize_failure_reason(doc.get("failure_reason")) if outcome == "failed" else None
        )
        when, sort_at = _time_fields_from_dt(_run_timestamp(doc))
        items.append(
            ActivityItem(
                kind=run_kind,
                id=str(doc.get("id", "")),
                summary=summary,
                when=when,
                sort_at=sort_at,
                outcome=outcome,
                delivery=None,
                source=source,
                failure_label=failure_label,
            )
        )
    return items


async def recent_activity(
    owner_id: str | None = None,
    *,
    limit: int = DEFAULT_ACTIVITY_LIMIT,
    kind: ActivityKind | None = None,
    include_user: bool = False,
) -> list[ActivityItem]:
    """Merge per-domain projectors into one time-sorted activity timeline.

    User-initiated turns are excluded by default (they're an opt-in Operations
    facet). Pass ``include_user=True`` to fold them into the unified timeline so
    the "All" view actually shows everything.
    """
    oid = owner_id or settings.DEFAULT_USER_ID
    item_limit = _normalize_limit(limit)
    projectors = []

    if kind is None or kind == "headless":
        projectors.append(_recent_headless_turns(oid, item_limit))
    if kind is None:
        projectors.append(_recent_runs(oid, item_limit))
    elif kind == "trigger":
        projectors.append(_recent_runs(oid, item_limit, kind="trigger"))
    elif kind == "automation":
        projectors.append(_recent_runs(oid, item_limit, kind="automation"))
    if kind is None or kind == "task":
        projectors.append(_recent_tasks(oid, item_limit))
    if kind == "user" or (kind is None and include_user):
        projectors.append(_recent_user_turns(oid, item_limit))

    groups = await asyncio.gather(*projectors) if projectors else []
    merged = [item for group in groups for item in group]

    def sort_key(item: ActivityItem) -> datetime:
        try:
            return datetime.fromisoformat(item.sort_at)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    merged.sort(key=sort_key, reverse=True)
    return merged[:item_limit]
