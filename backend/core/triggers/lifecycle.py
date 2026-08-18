"""Shared trigger lifecycle helpers for pause/disable and ownership guards."""

from __future__ import annotations

from datetime import datetime, timezone

from core.triggers.models import ManagementOwnership, TriggerRule
from services.database.mongodb import mongodb

OPEN_INSTANCE_STATUSES = ("pending", "awaiting_delivery")


def rule_allows_dispatch(rule: TriggerRule, *, now: datetime | None = None) -> bool:
    """Return whether a parent rule should permit claiming or dispatch."""
    now_utc = now or datetime.now(timezone.utc)
    if not rule.enabled:
        return False
    if rule.paused_until and rule.paused_until > now_utc:
        return False
    return True


def rule_doc_allows_dispatch(rule_doc: dict, *, now: datetime | None = None) -> bool:
    now_utc = now or datetime.now(timezone.utc)
    if not rule_doc.get("enabled", True):
        return False
    paused_until = rule_doc.get("paused_until")
    if paused_until is not None:
        from core.scheduling import coerce_datetime_or_none

        paused_at = coerce_datetime_or_none(paused_until)
        if paused_at and paused_at > now_utc:
            return False
    return True


def rule_management(rule_doc: dict) -> ManagementOwnership:
    return ManagementOwnership.model_validate(rule_doc["management"])


def is_scheduler_managed(rule_doc: dict) -> bool:
    management = rule_management(rule_doc)
    if management.provider != "scheduler":
        return False
    origin = rule_doc.get("origin") or {}
    return origin.get("kind") in {"time", "interval"} and rule_doc.get("surface") is True


def is_scheduler_managed_instance(instance_doc: dict) -> bool:
    management = ManagementOwnership.model_validate(instance_doc["management"])
    return management.provider == "scheduler"


async def cancel_open_instances_for_rule(
    owner_id: str,
    rule_id: str,
    *,
    reason: str | None = None,
) -> int:
    """Cancel pending or awaiting_delivery instances for one rule."""
    now = datetime.now(timezone.utc)
    update: dict[str, object] = {
        "status": "cancelled",
        "completed_at": now,
        "updated_at": now,
    }
    if reason:
        update["failure_reason"] = reason
    result = await mongodb.db.trigger_instances.update_many(
        {
            "owner_id": owner_id,
            "rule_id": rule_id,
            "status": {"$in": list(OPEN_INSTANCE_STATUSES)},
        },
        {"$set": update},
    )
    return int(result.modified_count)


async def materialize_after_pause(rule: TriggerRule, paused_until: datetime) -> None:
    """Materialize the first recurring occurrence after a finite pause."""
    if not rule.origin.recurrence:
        return
    from core.scheduling import next_occurrence, recurrence_rule_from_origin
    from core.triggers.service import trigger_service

    due_at = next_occurrence(
        recurrence_rule_from_origin(
            rule.origin.model_dump(mode="python", exclude_none=True),
            rule_doc=rule.model_dump(mode="python"),
            owner_id=rule.owner_id,
            rule_id=rule.id,
        ),
        paused_until,
    )
    if due_at is None:
        return
    await trigger_service.materialize_recurring_occurrence(
        owner_id=rule.owner_id,
        rule_id=rule.id,
        origin=rule.origin,
        action=rule.action,
        attention=rule.attention,
        delivery=rule.delivery,
        freshness=rule.freshness,
        due_at=due_at,
        management=rule.management,
    )
