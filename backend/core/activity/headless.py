"""Headless conversation rows: fetch, project, and group into activity items."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from core.activity.models import ActivityItem, ActivityTraceLine
from core.config import settings
from core.plugins.capabilities import capability_call_preview
from core.time import local_datetime_fields
from core.turns.visibility import HIDDEN_DELIVERIES
from services.database.mongodb import mongodb

SUMMARY_CHARS = 180
TOOL_SUMMARY_CHARS = 120
TRACE_CONTENT_CHARS = 2000


def project_headless_row(msg: dict) -> dict:
    """Map one DB message to a headless audit row dict."""
    meta = msg.get("metadata") or {}
    capability = meta.get("capability")
    arguments = meta.get("arguments") if isinstance(meta.get("arguments"), dict) else None
    preview = (
        capability_call_preview(str(capability), arguments or {})
        if capability
        else meta.get("code")
    )
    return {
        "timestamp": msg["timestamp"],
        "turn_id": meta.get("turn_id"),
        "delivery": meta.get("delivery", "unknown"),
        "role": msg["role"],
        "content": msg["content"],
        "turn_type": meta.get("turn_type"),
        "tool_call_id": meta.get("tool_call_id"),
        "code": preview,
        "capability": capability,
        "trigger_source": meta.get("trigger_source"),
        "rule_id": meta.get("rule_id"),
        "rule_name": meta.get("rule_name"),
        "protocol_name": meta.get("protocol_name"),
        "decision": meta.get("decision"),
        "instructions": meta.get("instructions"),
        "model": meta.get("model"),
    }


async def fetch_headless_activity_items(*, owner_id: str | None = None, limit: int) -> list[ActivityItem]:
    """Fetch recent headless turns with `limit` applied after turn grouping."""
    user_id = owner_id or settings.DEFAULT_USER_ID
    pipeline = [
        {
            "$match": {
                "owner_id": user_id,
                "source": "system",
                "metadata.delivery": {"$in": list(HIDDEN_DELIVERIES)},
                "content": {"$not": {"$regex": r"^\s*<tool_result>"}},
                "metadata.turn_type": {"$ne": "tool_result"},
            }
        },
        {"$sort": {"timestamp": -1}},
        {
            "$group": {
                "_id": {"$ifNull": ["$metadata.turn_id", {"$concat": ["row:", {"$toString": "$_id"}]}]},
                "latest_ts": {"$first": "$timestamp"},
                "rows": {
                    "$push": {
                        "timestamp": "$timestamp",
                        "turn_id": "$metadata.turn_id",
                        "delivery": "$metadata.delivery",
                        "role": "$role",
                        "content": "$content",
                        "turn_type": "$metadata.turn_type",
                        "tool_call_id": "$metadata.tool_call_id",
                        "code": {"$ifNull": ["$metadata.capability", "$metadata.code"]},
                        "trigger_source": "$metadata.trigger_source",
                        "rule_id": "$metadata.rule_id",
                        "rule_name": "$metadata.rule_name",
                        "protocol_name": "$metadata.protocol_name",
                        "decision": "$metadata.decision",
                        "instructions": "$metadata.instructions",
                        "model": "$metadata.model",
                    }
                },
            }
        },
        {"$sort": {"latest_ts": -1}},
        {"$limit": limit},
    ]
    docs = await mongodb.db.conversations.aggregate(pipeline).to_list(length=limit)
    rows = [
        {
            **row,
            "timestamp": row["timestamp"].isoformat() if isinstance(row.get("timestamp"), datetime) else row.get("timestamp"),
            "turn_id": row.get("turn_id") or doc["_id"],
        }
        for doc in docs
        for row in doc.get("rows", [])
    ]
    return headless_rows_to_activity_items(rows)


def _parse_timestamp(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _time_fields(ts: str) -> tuple[str, str]:
    fields = local_datetime_fields(_parse_timestamp(ts))
    return fields["time"], fields["utc_time"]


def _headless_source(meta_rows: list[dict]) -> str | None:
    for row in meta_rows:
        if row.get("rule_name"):
            return str(row["rule_name"])
        if row.get("protocol_name"):
            return str(row["protocol_name"])
        if row.get("trigger_source"):
            return str(row["trigger_source"])
    return None


def _headless_summary(rows: list[dict]) -> str:
    for row in rows:
        if row.get("turn_type") == "reasoning":
            continue
        if row.get("role") == "assistant" and row.get("content"):
            text = str(row["content"]).strip()
            if text and text != "NO_REPLY":
                return text[:SUMMARY_CHARS]
        if row.get("turn_type") == "tool_call" and (row.get("code") or row.get("capability")):
            return f"Tool run: {str(row.get('code') or row.get('capability'))[:TOOL_SUMMARY_CHARS]}"
    for row in rows:
        if row.get("content"):
            return str(row["content"]).strip()[:SUMMARY_CHARS]
    delivery = rows[0].get("delivery", "headless")
    return f"Headless turn ({delivery})"


def _delivery_tag(rows: list[dict]) -> str | None:
    delivery = rows[0].get("delivery")
    if delivery in ("silent", "suppressed", "announce", "evaluate", "prefetched"):
        return delivery
    return None


def headless_rows_to_activity_items(rows: list[dict]) -> list[ActivityItem]:
    """Group projected headless rows by turn_id into ActivityItem entries."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = row.get("turn_id") or f"row:{row.get('timestamp', '')}"
        groups[key].append(row)

    items: list[ActivityItem] = []
    for turn_id, group_rows in groups.items():
        group_rows.sort(key=lambda r: r.get("timestamp", ""))
        latest_ts = group_rows[-1]["timestamp"]
        when, sort_at = _time_fields(latest_ts)
        delivery = _delivery_tag(group_rows)
        trace = [
            ActivityTraceLine(
                role=str(r.get("role", "")),
                content=str(r.get("content", ""))[:TRACE_CONTENT_CHARS],
                turn_type=r.get("turn_type"),
            )
            for r in group_rows
            if r.get("content") or r.get("code")
        ] or None
        items.append(
            ActivityItem(
                kind="headless",
                id=str(turn_id),
                summary=_headless_summary(group_rows),
                when=when,
                sort_at=sort_at,
                outcome="completed",
                delivery=delivery,  # type: ignore[arg-type]
                source=_headless_source(group_rows),
                trace=trace,
            )
        )
    return items
