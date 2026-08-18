"""Cursor-paginated activity projection over canonical domain stores."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from core.activity.models import (
    ActivityCategory,
    ActivityDetailRef,
    ActivityEntry,
    ActivityPage,
    ActivityPageOutcome,
)
from core.triggers.projection import trigger_run_source
from core.triggers.vocabulary import humanize_failure_reason
from services.database.mongodb import mongodb

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 100
# Conversations are opt-in via category="conversation"; All is operational only.
_DEFAULT_SOURCES = ("reminder", "automation", "task", "system")


class ActivityQuery(BaseModel):
    category: ActivityCategory | None = None
    outcome: ActivityPageOutcome | None = None
    source: str | None = None
    node_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    search: str | None = Field(default=None, max_length=120)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]


class _Key(BaseModel):
    occurred_at: datetime
    activity_id: str


class _Cursor(BaseModel):
    version: int = 1
    query: str
    positions: dict[str, _Key] = Field(default_factory=dict)


@dataclass
class _SourcePage:
    source: str
    items: list[ActivityEntry]
    has_more: bool


def _encode_cursor(cursor: _Cursor) -> str:
    payload = cursor.model_dump_json(exclude_none=True).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str | None, query: ActivityQuery) -> _Cursor:
    if not value:
        return _Cursor(query=query.fingerprint())
    try:
        padded = value + "=" * (-len(value) % 4)
        cursor = _Cursor.model_validate_json(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise ValueError("Invalid activity cursor") from exc
    if cursor.version != 1 or cursor.query != query.fingerprint():
        raise ValueError("Activity cursor does not match the current filters")
    return cursor


def _source_key(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or None


def _source_label(value: str | None, fallback: str) -> str:
    if not value:
        return fallback
    return re.sub(r"[_-]+", " ", value).strip().title()


def _key_filter(
    timestamp_field: str,
    id_field: str,
    position: _Key | None,
    prefix: str,
) -> dict[str, Any]:
    if position is None:
        return {}
    cursor_prefix, _, cursor_raw_id = position.activity_id.partition(":")
    if prefix < cursor_prefix:
        return {timestamp_field: {"$lte": position.occurred_at}}
    if prefix > cursor_prefix:
        return {timestamp_field: {"$lt": position.occurred_at}}
    return {
        "$or": [
            {timestamp_field: {"$lt": position.occurred_at}},
            {
                timestamp_field: position.occurred_at,
                id_field: {"$lt": cursor_raw_id},
            },
        ]
    }


def _apply_time(query: dict[str, Any], field: str, request: ActivityQuery) -> None:
    bounds: dict[str, datetime] = {}
    if request.since:
        bounds["$gte"] = request.since
    if request.until:
        bounds["$lte"] = request.until
    if bounds:
        existing = query.get(field)
        if isinstance(existing, dict):
            query[field] = {**existing, **bounds}
        else:
            query[field] = bounds


def _outcome_statuses(outcome: ActivityPageOutcome | None) -> list[str] | None:
    if outcome == "running":
        return ["claimed", "executing"]
    if outcome == "waiting":
        return ["awaiting_delivery"]
    if outcome == "suppressed":
        return ["suppressed"]
    if outcome == "cancelled":
        return ["cancelled", "expired"]
    if outcome == "failed":
        return ["failed"]
    if outcome == "succeeded":
        return ["completed", "delivered", "acknowledged"]
    return None


def _run_outcome(status: str) -> ActivityPageOutcome:
    if status in {"claimed", "executing"}:
        return "running"
    if status == "awaiting_delivery":
        return "waiting"
    if status == "suppressed":
        return "suppressed"
    if status in {"cancelled", "expired"}:
        return "cancelled"
    if status == "failed":
        return "failed"
    return "succeeded"


async def _conversation_page(
    owner_id: str,
    request: ActivityQuery,
    position: _Key | None,
    limit: int,
) -> _SourcePage:
    query: dict[str, Any] = {
        "owner_id": owner_id,
        "role": "user",
        "source": "user",
        "metadata.turn_id": {"$exists": True},
        "metadata.turn_type": {"$exists": False},
        **_key_filter("timestamp", "metadata.turn_id", position, "conversation"),
    }
    if request.node_id:
        query["metadata.node_id"] = request.node_id
    if request.source:
        query["$and"] = query.get("$and", []) + [
            {
                "$or": [
                    {"metadata.node_id": request.source},
                    {"metadata.node_label": {"$regex": re.escape(request.source), "$options": "i"}},
                ]
            }
        ]
    if request.search:
        query["content"] = {"$regex": re.escape(request.search), "$options": "i"}
    _apply_time(query, "timestamp", request)

    pipeline: list[dict[str, Any]] = [{"$match": query}]
    if request.outcome is None:
        pipeline.extend([
            {"$sort": {"timestamp": -1, "metadata.turn_id": -1}},
            {"$limit": limit + 1},
        ])
    pipeline.extend([
        {
            "$lookup": {
                "from": "turn_runs",
                "let": {"turn_id": "$metadata.turn_id"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$owner_id", owner_id]},
                                    {"$eq": ["$turn_id", "$$turn_id"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "status": 1, "node_id": 1, "node_label": 1}},
                    {"$limit": 1},
                ],
                "as": "_perf",
            }
        },
        {"$set": {"_perf": {"$first": "$_perf"}}},
    ])
    if request.outcome:
        statuses = {
            "running": ["running", "pending", "handoff"],
            "cancelled": ["cancelled"],
            "failed": ["failed"],
            "succeeded": ["completed"],
        }.get(request.outcome)
        if not statuses:
            return _SourcePage("conversation", [], False)
        pipeline.extend([
            {
                "$set": {
                    "_resolved_status": {
                        "$ifNull": ["$_perf.status", {"$ifNull": ["$metadata.turn_status", "completed"]}]
                    }
                }
            },
            {"$match": {"_resolved_status": {"$in": statuses}}},
        ])
    if request.outcome is not None:
        pipeline.extend([
            {"$sort": {"timestamp": -1, "metadata.turn_id": -1}},
            {"$limit": limit + 1},
        ])
    pipeline.append({"$project": {"_id": 0}})
    docs = await mongodb.db.conversations.aggregate(pipeline).to_list(length=limit + 1)
    page_docs = docs[:limit]

    items: list[ActivityEntry] = []
    for doc in page_docs:
        meta = doc.get("metadata") or {}
        turn_id = str(meta.get("turn_id", ""))
        perf = doc.get("_perf") or {}
        status = str(perf.get("status") or meta.get("turn_status") or "completed")
        outcome: ActivityPageOutcome = (
            "running" if status in {"running", "pending", "handoff"} else
            "cancelled" if status == "cancelled" else
            "failed" if status == "failed" else
            "succeeded"
        )
        node_id = perf.get("node_id") or meta.get("node_id")
        node_label = perf.get("node_label") or meta.get("node_label") or node_id
        content = str(doc.get("content", "")).strip()
        items.append(
            ActivityEntry(
                activity_id=f"conversation:{turn_id}",
                category="conversation",
                occurred_at=doc["timestamp"],
                outcome=outcome,
                title=content[:180] or "Conversation",
                source_key=_source_key(str(node_id)) if node_id else "user",
                source_label=str(node_label) if node_label else "User",
                detail_ref=ActivityDetailRef(kind="turn", id=turn_id),
                turn_id=turn_id,
                node_id=str(node_id) if node_id else None,
            )
        )
    return _SourcePage("conversation", items, len(docs) > limit)


async def _run_page(
    owner_id: str,
    request: ActivityQuery,
    position: _Key | None,
    limit: int,
    *,
    category: ActivityCategory,
) -> _SourcePage:
    prefix = category
    statuses = _outcome_statuses(request.outcome)
    query: dict[str, Any] = {
        "owner_id": owner_id,
        **_key_filter("updated_at", "id", position, prefix),
    }
    if statuses:
        query["status"] = {"$in": statuses}
    if category == "automation":
        query.update({"origin_snapshot.kind": "external", "source_event.rule_id": {"$exists": True}})
    else:
        query["$and"] = query.get("$and", []) + [
            {
                "$or": [
                    {"origin_snapshot.kind": {"$ne": "external"}},
                    {"source_event.rule_id": {"$exists": False}},
                ]
            }
        ]
    if request.source:
        escaped = re.escape(request.source)
        query["$and"] = query.get("$and", []) + [
            {
                "$or": [
                    {"source_event.rule_name": {"$regex": escaped, "$options": "i"}},
                    {"origin_snapshot.source": {"$regex": escaped, "$options": "i"}},
                    {"action_snapshot.protocol_name": {"$regex": escaped, "$options": "i"}},
                ]
            }
        ]
    if request.search:
        escaped = re.escape(request.search)
        query["$and"] = query.get("$and", []) + [
            {
                "$or": [
                    {"result_text": {"$regex": escaped, "$options": "i"}},
                    {"action_snapshot.message": {"$regex": escaped, "$options": "i"}},
                    {"source_event.rule_name": {"$regex": escaped, "$options": "i"}},
                ]
            }
        ]
    _apply_time(query, "updated_at", request)
    docs = await (
        mongodb.db.trigger_instances.find(query, {"_id": 0})
        .sort([("updated_at", -1), ("id", -1)])
        .limit(limit + 1)
        .to_list(length=limit + 1)
    )

    items: list[ActivityEntry] = []
    for doc in docs[:limit]:
        action = doc.get("action_snapshot") or {}
        source = trigger_run_source(doc)
        status = str(doc.get("status", ""))
        result = str(doc.get("result_text") or action.get("message") or "").strip()
        label = "Automation" if category == "automation" else "Reminder"
        title = result[:180] or f"{label} {status.replace('_', ' ')}"
        instance_id = str(doc.get("id", ""))
        rule_id = doc.get("rule_id") or (doc.get("source_event") or {}).get("rule_id")
        items.append(
            ActivityEntry(
                activity_id=f"{prefix}:{instance_id}",
                category=category,
                occurred_at=doc.get("updated_at") or doc.get("created_at") or datetime.now(timezone.utc),
                updated_at=doc.get("updated_at"),
                outcome=_run_outcome(status),
                title=title,
                source_key=_source_key(source) or prefix,
                source_label=_source_label(source, label),
                detail_ref=ActivityDetailRef(kind="trigger_instance", id=instance_id),
                instance_id=instance_id,
                rule_id=str(rule_id) if rule_id else None,
                failure_label=humanize_failure_reason(doc.get("failure_reason")),
            )
        )
    return _SourcePage(prefix, items, len(docs) > limit)


async def _task_page(
    owner_id: str,
    request: ActivityQuery,
    position: _Key | None,
    limit: int,
) -> _SourcePage:
    query: dict[str, Any] = {
        "owner_id": owner_id,
        **_key_filter("created_at", "task_id", position, "task"),
    }
    if request.outcome:
        statuses = {
            "running": ["running"],
            "failed": ["failed"],
            "cancelled": ["cancelled"],
            "succeeded": ["completed"],
        }.get(request.outcome, [])
        if not statuses:
            return _SourcePage("task", [], False)
        query["status"] = {"$in": statuses}
    if request.source:
        query["source"] = {"$regex": re.escape(request.source), "$options": "i"}
    if request.search:
        escaped = re.escape(request.search)
        query["$and"] = query.get("$and", []) + [
            {
                "$or": [
                    {"progress_summary": {"$regex": escaped, "$options": "i"}},
                    {"prompt": {"$regex": escaped, "$options": "i"}},
                ]
            }
        ]
    _apply_time(query, "created_at", request)
    docs = await (
        mongodb.db.background_tasks.find(query, {"_id": 0, "events": 0, "trace": 0})
        .sort([("created_at", -1), ("task_id", -1)])
        .limit(limit + 1)
        .to_list(length=limit + 1)
    )
    items: list[ActivityEntry] = []
    for doc in docs[:limit]:
        task_id = str(doc.get("task_id", ""))
        status = str(doc.get("status", "running"))
        outcome: ActivityPageOutcome = (
            "running" if status == "running" else
            "failed" if status == "failed" else
            "cancelled" if status == "cancelled" else
            "succeeded"
        )
        source = str(doc.get("source") or doc.get("mode") or "agent")
        items.append(
            ActivityEntry(
                activity_id=f"task:{task_id}",
                category="task",
                occurred_at=doc.get("created_at") or datetime.now(timezone.utc),
                updated_at=doc.get("completed_at"),
                outcome=outcome,
                title=str(doc.get("progress_summary") or doc.get("prompt") or "Background task")[:180],
                source_key=_source_key(source) or "agent",
                source_label=_source_label(source, "Agent"),
                detail_ref=ActivityDetailRef(kind="background_task", id=task_id),
                task_id=task_id,
            )
        )
    return _SourcePage("task", items, len(docs) > limit)


async def _system_page(
    owner_id: str,
    request: ActivityQuery,
    position: _Key | None,
    limit: int,
) -> _SourcePage:
    if request.outcome and request.outcome not in {"succeeded", "suppressed"}:
        return _SourcePage("system", [], False)
    match: dict[str, Any] = {
        "owner_id": owner_id,
        "source": "system",
        "metadata.delivery": {
            "$in": (
                ["silent"] if request.outcome == "succeeded" else
                ["suppressed"] if request.outcome == "suppressed" else
                ["silent", "suppressed"]
            )
        },
        "metadata.instance_id": {"$exists": False},
        "content": {"$not": {"$regex": r"^\s*<tool_result>"}},
        "metadata.turn_type": {"$ne": "tool_result"},
    }
    if request.node_id:
        match["metadata.node_id"] = request.node_id
    if request.source:
        escaped = re.escape(request.source)
        match["$or"] = [
            {"metadata.rule_name": {"$regex": escaped, "$options": "i"}},
            {"metadata.protocol_name": {"$regex": escaped, "$options": "i"}},
            {"metadata.trigger_source": {"$regex": escaped, "$options": "i"}},
        ]
    if request.search:
        match["content"] = {
            "$not": {"$regex": r"^\s*<tool_result>"},
            "$regex": re.escape(request.search),
            "$options": "i",
        }
    _apply_time(match, "timestamp", request)
    pipeline: list[dict[str, Any]] = [
        {"$match": match},
        {"$sort": {"timestamp": -1}},
        {
            "$group": {
                "_id": {"$ifNull": ["$metadata.turn_id", {"$toString": "$_id"}]},
                "occurred_at": {"$first": "$timestamp"},
                "delivery": {"$first": "$metadata.delivery"},
                "content": {"$first": "$content"},
                "rule_name": {"$first": "$metadata.rule_name"},
                "protocol_name": {"$first": "$metadata.protocol_name"},
                "trigger_source": {"$first": "$metadata.trigger_source"},
                "node_id": {"$first": "$metadata.node_id"},
            }
        },
    ]
    if position:
        cursor_prefix, _, raw_id = position.activity_id.partition(":")
        if "system" < cursor_prefix:
            pipeline.append({"$match": {"occurred_at": {"$lte": position.occurred_at}}})
        elif "system" > cursor_prefix:
            pipeline.append({"$match": {"occurred_at": {"$lt": position.occurred_at}}})
        else:
            pipeline.append({
                "$match": {
                    "$or": [
                        {"occurred_at": {"$lt": position.occurred_at}},
                        {"occurred_at": position.occurred_at, "_id": {"$lt": raw_id}},
                    ]
                }
            })
    pipeline.extend([
        {"$sort": {"occurred_at": -1, "_id": -1}},
        {"$limit": limit + 1},
    ])
    docs = await mongodb.db.conversations.aggregate(pipeline).to_list(length=limit + 1)
    items: list[ActivityEntry] = []
    for doc in docs[:limit]:
        delivery = doc.get("delivery")
        outcome: ActivityPageOutcome = "suppressed" if delivery == "suppressed" else "succeeded"
        turn_id = str(doc["_id"])
        source = doc.get("rule_name") or doc.get("protocol_name") or doc.get("trigger_source")
        content = str(doc.get("content") or "").strip()
        items.append(
            ActivityEntry(
                activity_id=f"system:{turn_id}",
                category="system",
                occurred_at=doc["occurred_at"],
                outcome=outcome,
                title=content[:180] if content and content != "NO_REPLY" else "Background evaluation",
                source_key=_source_key(str(source)) if source else "system",
                source_label=_source_label(str(source) if source else None, "System"),
                delivery=delivery,
                detail_ref=ActivityDetailRef(kind="turn", id=turn_id),
                turn_id=turn_id,
                node_id=doc.get("node_id"),
            )
        )
    return _SourcePage("system", items, len(docs) > limit)


async def activity_page(
    owner_id: str,
    *,
    query: ActivityQuery | None = None,
    cursor: str | None = None,
    limit: int = PAGE_SIZE_DEFAULT,
) -> ActivityPage:
    if limit < 1 or limit > PAGE_SIZE_MAX:
        raise ValueError(f"Activity page limit must be between 1 and {PAGE_SIZE_MAX}")
    request = query or ActivityQuery()
    state = _decode_cursor(cursor, request)
    selected = [request.category] if request.category else list(_DEFAULT_SOURCES)

    loaders = {
        "conversation": _conversation_page,
        "reminder": lambda oid, req, pos, lim: _run_page(
            oid, req, pos, lim, category="reminder"
        ),
        "automation": lambda oid, req, pos, lim: _run_page(
            oid, req, pos, lim, category="automation"
        ),
        "task": _task_page,
        "system": _system_page,
    }
    import asyncio

    pages = await asyncio.gather(
        *[
            loaders[source](owner_id, request, state.positions.get(source), limit)
            for source in selected
        ]
    )
    candidates = sorted(
        (item for page in pages for item in page.items),
        key=lambda item: (item.occurred_at, item.activity_id),
        reverse=True,
    )
    items = candidates[:limit]
    consumed_by_source: dict[str, ActivityEntry] = {}
    for item in items:
        consumed_by_source[item.category] = item
    for source, item in consumed_by_source.items():
        state.positions[source] = _Key(
            occurred_at=item.occurred_at,
            activity_id=item.activity_id,
        )

    has_more = len(candidates) > limit or any(page.has_more for page in pages)
    return ActivityPage(
        items=items,
        next_cursor=_encode_cursor(state) if has_more and items else None,
        has_more=has_more and bool(items),
    )
