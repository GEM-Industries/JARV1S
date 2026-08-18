"""Bounded current-state read model for evaluate offer decisions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from core.time import coerce_datetime_or_none, local_datetime_fields
from core.triggers.freshness import freshness_expiry_reason
from core.triggers.priority import commitment_attention_mongo_filter
from core.triggers.models import FreshnessPolicy, TriggerInstance
from services.database.mongodb import mongodb

_COMMITMENT_STATUSES = ("pending", "claimed", "executing", "awaiting_delivery")
_COMMITMENT_LOOKAHEAD = timedelta(hours=24)


def validate_offer_defer_retry_at(
    retry_at: datetime | None,
    instance: TriggerInstance,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Accept AI-selected retry times that are in the future and still fresh."""
    if retry_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    if retry_at <= now:
        return None
    due_at = coerce_datetime_or_none(instance.due_at)
    if freshness_expiry_reason(instance, now=retry_at, due_at=due_at):
        return None
    return retry_at


def resolve_offer_defer_retry_at(
    requested: datetime | None,
    instance: TriggerInstance,
    *,
    fallback: datetime,
    now: datetime | None = None,
) -> datetime:
    return validate_offer_defer_retry_at(requested, instance, now=now) or fallback


async def assemble_offer_state(
    instance: TriggerInstance,
    *,
    timezone_name: str = "UTC",
    now: datetime | None = None,
) -> str:
    """Assemble live state for an evaluate offer decision."""
    owner_id = instance.owner_id
    now = now or datetime.now(timezone.utc)
    parts: list[str] = []

    due_at = coerce_datetime_or_none(instance.due_at)
    if due_at:
        age_minutes = max(0, int((now - due_at).total_seconds() / 60))
        parts.append(f"TRIGGER_AGE_MINUTES: {age_minutes}")
    else:
        parts.append("TRIGGER_AGE_MINUTES: unknown")

    commitments = await _fetch_active_commitments(
        owner_id,
        now=now,
        timezone_name=timezone_name,
    )
    if commitments:
        lines = [f"  - {row['summary']}" for row in commitments]
        parts.append("ACTIVE_COMMITMENTS:\n" + "\n".join(lines))
    else:
        parts.append("ACTIVE_COMMITMENTS: none")

    return "\n\n".join(parts)


def _format_relative_delta(delta: timedelta) -> str:
    """Compact relative age for prompt-only state, not user-facing tool output."""
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins, rem_secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{rem_secs}s" if rem_secs else f"{mins}m"
    hours, rem_mins = divmod(mins, 60)
    return f"{hours}h{rem_mins}m" if rem_mins else f"{hours}h"


def _format_commitment_summary(
    doc: dict,
    *,
    now: datetime,
    timezone_name: str,
) -> str:
    action = doc.get("action_snapshot") or {}
    attention = doc.get("attention_snapshot") or {}
    origin = doc.get("origin_snapshot") or {}
    delivery = doc.get("delivery_snapshot") or {}
    sound = str(attention.get("sound") or "chime")
    status = str(doc.get("status") or "unknown")
    decision = str(action.get("decision") or "").strip()
    level = str(attention.get("level") or "").strip()
    origin_kind = str(origin.get("kind") or "").strip()
    requires_ack = attention.get("requires_ack")
    message = str(action.get("message") or "").strip()

    due = coerce_datetime_or_none(doc.get("due_at"))
    if due:
        due_clock = local_datetime_fields(due, timezone_name=timezone_name)["local_time"]
        if due > now:
            due_part = f"due {due_clock}, in {_format_relative_delta(due - now)}"
        else:
            due_part = f"due {due_clock}, {_format_relative_delta(now - due)} ago"
    else:
        due_part = "due unknown"

    status_part = f"{status} {sound}"
    details: list[str] = []
    if level:
        details.append(f"level={level}")
    if requires_ack is not None:
        details.append(f"requires_ack={str(requires_ack).lower()}")
    if decision:
        details.append(f"decision={decision}")
    if origin_kind:
        details.append(f"origin={origin_kind}")
    target = delivery.get("target")
    if target:
        details.append(f"target={target}")
    if details:
        status_part = f"{status_part} ({', '.join(details)})"

    parts = [f"{doc['id']}", status_part, due_part]

    created = coerce_datetime_or_none(doc.get("created_at"))
    updated = coerce_datetime_or_none(doc.get("updated_at"))
    if updated and created and updated != created:
        updated_clock = local_datetime_fields(updated, timezone_name=timezone_name)["local_time"]
        parts.append(f"updated {updated_clock}, {_format_relative_delta(now - updated)} ago")
    elif created:
        created_clock = local_datetime_fields(created, timezone_name=timezone_name)["local_time"]
        parts.append(f"created {created_clock}, {_format_relative_delta(now - created)} ago")

    summary = "; ".join(parts)
    if message:
        summary = f"{summary} — {message}"
    return summary


async def _fetch_active_commitments(
    owner_id: str,
    *,
    now: datetime,
    timezone_name: str,
) -> list[dict[str, str]]:
    cursor = mongodb.db.trigger_instances.find(
        {
            "owner_id": owner_id,
            "$and": [
                {"status": {"$in": list(_COMMITMENT_STATUSES)}},
                commitment_attention_mongo_filter(),
            ],
            "due_at": {"$lte": now + _COMMITMENT_LOOKAHEAD},
        },
        projection={
            "_id": 0,
            "id": 1,
            "rule_id": 1,
            "status": 1,
            "due_at": 1,
            "created_at": 1,
            "updated_at": 1,
            "action_snapshot.decision": 1,
            "action_snapshot.message": 1,
            "attention_snapshot.level": 1,
            "attention_snapshot.requires_ack": 1,
            "attention_snapshot.sound": 1,
            "delivery_snapshot.target": 1,
            "freshness_snapshot": 1,
            "origin_snapshot.kind": 1,
            "source_event": 1,
        },
        sort=[("due_at", 1)],
    )
    docs = await cursor.to_list(None)
    active_rule_ids = {
        doc.get("rule_id")
        for doc in docs
        if doc.get("rule_id") and doc.get("status") != "awaiting_delivery"
    }
    latest_awaiting_by_rule = _latest_awaiting_by_rule(docs)
    rows: list[dict[str, str]] = []
    for doc in docs:
        if not _include_commitment_doc(
            doc,
            now=now,
            active_rule_ids=active_rule_ids,
            latest_awaiting_by_rule=latest_awaiting_by_rule,
        ):
            continue
        rows.append({
            "summary": _format_commitment_summary(
                doc,
                now=now,
                timezone_name=timezone_name,
            ),
        })
    return rows


def _latest_awaiting_by_rule(docs: list[dict]) -> dict[str, str]:
    latest: dict[str, tuple[datetime, str]] = {}
    for doc in docs:
        rule_id = doc.get("rule_id")
        if doc.get("status") != "awaiting_delivery" or not rule_id:
            continue
        due = coerce_datetime_or_none(doc.get("due_at")) or datetime.min.replace(tzinfo=timezone.utc)
        current = latest.get(rule_id)
        if current is None or due >= current[0]:
            latest[rule_id] = (due, doc["id"])
    return {rule_id: instance_id for rule_id, (_due, instance_id) in latest.items()}


def _include_commitment_doc(
    doc: dict,
    *,
    now: datetime,
    active_rule_ids: set[str],
    latest_awaiting_by_rule: dict[str, str],
) -> bool:
    if doc.get("status") != "awaiting_delivery":
        return True

    rule_id = doc.get("rule_id")
    if rule_id and rule_id in active_rule_ids:
        return False
    if rule_id and latest_awaiting_by_rule.get(rule_id) != doc.get("id"):
        return False

    freshness = FreshnessPolicy.model_validate(doc.get("freshness_snapshot") or {})
    instance = SimpleNamespace(
        freshness_snapshot=freshness,
        source_event=doc.get("source_event") or {},
    )
    due = coerce_datetime_or_none(doc.get("due_at"))
    return freshness_expiry_reason(instance, now=now, due_at=due) is None
