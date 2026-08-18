"""Schedule definition API routes backed by trigger rules."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.deps.device_auth import require_owner_id
from core.scheduling import coerce_datetime_or_none, describe
from services.database.mongodb import mongodb

router = APIRouter(prefix="/schedules", tags=["schedules"])


class ScheduleSummary(BaseModel):
    id: str
    name: str
    enabled: bool
    origin_kind: str | None = None
    recurrence: str | None = None
    recurrence_label: str | None = None
    next_due_at: datetime | None = None
    decision: str | None = None
    paused_until: datetime | None = None
    updated_at: datetime | None = None


@router.get("/", response_model=list[ScheduleSummary])
async def list_schedules(
    include_disabled: bool = False,
    owner_id: str = Depends(require_owner_id),
) -> list[ScheduleSummary]:
    """List scheduled trigger rules for the authenticated owner (active only by default)."""
    query: dict[str, Any] = {
        "owner_id": owner_id,
        "origin.kind": {"$in": ["time", "interval"]},
        "surface": True,
    }
    if not include_disabled:
        query["enabled"] = True

    cursor = mongodb.db.trigger_rules.find(
        query,
        {
            "_id": 0,
            "id": 1,
            "name": 1,
            "description": 1,
            "enabled": 1,
            "surface": 1,
            "origin": 1,
            "action": 1,
            "paused_until": 1,
            "updated_at": 1,
        },
    ).sort("updated_at", -1)

    docs = await cursor.to_list(length=200)
    next_due_by_rule = await _next_due_by_rule(owner_id, [str(doc["id"]) for doc in docs if doc.get("id")])

    summaries: list[ScheduleSummary] = []
    for doc in docs:
        origin = doc.get("origin") if isinstance(doc.get("origin"), dict) else {}
        action = doc.get("action") if isinstance(doc.get("action"), dict) else {}
        rule_id = str(doc.get("id", ""))
        summaries.append(
            ScheduleSummary(
                id=rule_id,
                name=str(doc.get("name", "Schedule")),
                enabled=bool(doc.get("enabled", False)),
                origin_kind=origin.get("kind"),
                recurrence=origin.get("recurrence"),
                recurrence_label=_recurrence_label(origin),
                next_due_at=next_due_by_rule.get(rule_id),
                decision=action.get("decision"),
                paused_until=doc.get("paused_until"),
                updated_at=doc.get("updated_at"),
            )
        )
    return summaries


async def _next_due_by_rule(owner_id: str, rule_ids: list[str]) -> dict[str, datetime]:
    """Map each rule to its soonest pending occurrence, if one is scheduled."""
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

    next_due: dict[str, datetime] = {}
    for instance in await cursor.to_list(length=500):
        rule_id = str(instance.get("rule_id", ""))
        due_at = coerce_datetime_or_none(instance.get("due_at"))
        if rule_id and due_at and rule_id not in next_due:
            next_due[rule_id] = due_at
    return next_due


def _recurrence_label(origin: dict[str, Any]) -> str | None:
    recurrence = origin.get("recurrence")
    if not recurrence:
        return None
    return describe(
        {
            "recurrence": recurrence,
            "original_local_time": origin.get("original_local_time"),
            "timezone": origin.get("timezone"),
            "trigger_time": origin.get("fire_at"),
        }
    )
