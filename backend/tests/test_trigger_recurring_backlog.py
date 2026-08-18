"""Recurring trigger backlog collapse and guarded materialization."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    ManagementOwnership,
    TriggerAction,
    TriggerInstance,
    TriggerOrigin,
)
from core.triggers.service import TriggerService, schedule_dedup_key


def _instance(
    *,
    instance_id: str,
    rule_id: str | None,
    due_at: datetime,
) -> TriggerInstance:
    return TriggerInstance(
        id=instance_id,
        rule_id=rule_id,
        owner_id="geoff",
        status="awaiting_delivery",
        due_at=due_at,
        created_at=due_at,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=due_at),
        action_snapshot=TriggerAction(decision="tell", message="Wake up"),
        attention_snapshot=AttentionPolicy(level="critical", sound="alarm", requires_ack=True),
        delivery_snapshot=DeliveryPlan(),
        freshness_snapshot=FreshnessPolicy(),
        management={
            "provider": "scheduler",
            "resource_id": rule_id or instance_id,
        },
    )


@pytest.mark.asyncio
async def test_dedupe_awaiting_for_retry_keeps_latest_per_rule():
    now = datetime.now(timezone.utc)
    older = _instance(instance_id="trg-old", rule_id="rule-wake", due_at=now - timedelta(days=2))
    newer = _instance(instance_id="trg-new", rule_id="rule-wake", due_at=now - timedelta(days=1))
    one_shot = _instance(instance_id="trg-shot", rule_id=None, due_at=now - timedelta(hours=1))
    svc = TriggerService()
    expire = AsyncMock(return_value=True)

    with patch.object(svc, "expire_instance", expire):
        selected = await svc.dedupe_awaiting_for_retry([older, newer, one_shot])

    assert {inst.id for inst in selected} == {"trg-new", "trg-shot"}
    expire.assert_awaited_once_with("trg-old", reason="superseded_by_newer_occurrence")


@pytest.mark.asyncio
async def test_materialize_recurring_occurrence_skips_when_pending_exists():
    now = datetime.now(timezone.utc)
    svc = TriggerService()
    has_pending = AsyncMock(return_value=True)
    create_instance = AsyncMock()

    with patch.object(svc, "has_pending_for_rule", has_pending), patch.object(
        svc, "create_instance", create_instance
    ):
        created = await svc.materialize_recurring_occurrence(
            owner_id="geoff",
            rule_id="rule-wake",
            origin=TriggerOrigin(kind="time", fire_at=now),
            action=TriggerAction(decision="tell", message="Wake up"),
            attention=AttentionPolicy(),
            delivery=DeliveryPlan(),
            freshness=FreshnessPolicy(),
            due_at=now + timedelta(days=1),
            management=ManagementOwnership(provider="scheduler", resource_id="rule-wake"),
        )

    assert created is None
    create_instance.assert_not_awaited()


@pytest.mark.asyncio
async def test_materialize_recurring_occurrence_uses_schedule_dedup_key():
    now = datetime.now(timezone.utc)
    due = now + timedelta(days=1)
    svc = TriggerService()
    created = SimpleNamespace(id="trg-next")
    create_instance = AsyncMock(return_value=created)

    with patch.object(svc, "has_pending_for_rule", AsyncMock(return_value=False)), patch.object(
        svc, "create_instance", create_instance
    ):
        result = await svc.materialize_recurring_occurrence(
            owner_id="geoff",
            rule_id="rule-wake",
            origin=TriggerOrigin(kind="time", fire_at=now),
            action=TriggerAction(decision="tell", message="Wake up"),
            attention=AttentionPolicy(),
            delivery=DeliveryPlan(),
            freshness=FreshnessPolicy(),
            due_at=due,
            management=ManagementOwnership(provider="scheduler", resource_id="rule-wake"),
        )

    assert result is created
    assert create_instance.await_args.kwargs["dedup_key"] == schedule_dedup_key("rule-wake", due)


@pytest.mark.asyncio
async def test_supersede_awaiting_for_rule_expires_siblings():
    svc = TriggerService()
    collection = SimpleNamespace(update_many=AsyncMock(return_value=SimpleNamespace(modified_count=2)))
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        count = await svc.supersede_awaiting_for_rule(
            "rule-wake",
            keep_instance_id="trg-new",
            reason="superseded_by_settled_occurrence",
        )

    assert count == 2
    update_filter, update_op = collection.update_many.await_args.args
    assert update_filter == {
        "rule_id": "rule-wake",
        "status": "awaiting_delivery",
        "id": {"$ne": "trg-new"},
    }
    assert update_op["$set"]["status"] == "expired"
    assert update_op["$set"]["failure_reason"] == "superseded_by_settled_occurrence"
