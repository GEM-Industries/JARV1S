"""Tests for the pure trigger due decision helper."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.triggers.due_decision import resolve_trigger_due_decision
from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    TriggerAction,
    TriggerInstance,
    TriggerOrigin,
)


_NOW = datetime(2026, 7, 2, 12, tzinfo=timezone.utc)


def _instance(
    *,
    decision: str = "tell",
    level: str = "normal",
    sound: str = "chime",
    delivery: DeliveryPlan | None = None,
    freshness: FreshnessPolicy | None = None,
    origin_kind: str = "time",
    due_at: datetime = _NOW,
) -> TriggerInstance:
    return TriggerInstance(
        id="trg-1",
        owner_id="owner-1",
        status="claimed",
        due_at=due_at,
        created_at=_NOW,
        origin_snapshot=TriggerOrigin(kind=origin_kind, fire_at=due_at),
        action_snapshot=TriggerAction(decision=decision, message="Ping"),
        attention_snapshot=AttentionPolicy(level=level, sound=sound),
        delivery_snapshot=delivery or DeliveryPlan(),
        freshness_snapshot=freshness or FreshnessPolicy(),
        management={"provider": "scheduler", "resource_id": "trg-1"},
    )

def test_expire_when_freshness_expired_and_not_forced() -> None:
    instance = _instance(
        freshness=FreshnessPolicy(expires_at=_NOW - timedelta(minutes=1), on_expiry="expire"),
    )
    decision = resolve_trigger_due_decision(
        instance=instance, attention_mode="active", now=_NOW
    )
    assert decision.kind == "expire"
    assert decision.reason == "freshness_expired"


def test_force_delivery_when_on_expiry_force_deliver() -> None:
    instance = _instance(
        freshness=FreshnessPolicy(expires_at=_NOW - timedelta(minutes=1), on_expiry="force_deliver"),
    )
    decision = resolve_trigger_due_decision(
        instance=instance, attention_mode="active", now=_NOW
    )
    assert decision.kind == "execute"
    assert decision.force_delivery_reason == "freshness_expired"
    assert decision.delivery_resolution is not None


def test_quiet_attention_defers_tell_delivery() -> None:
    instance = _instance(decision="tell")
    decision = resolve_trigger_due_decision(
        instance=instance, attention_mode="quiet", now=_NOW
    )
    assert decision.kind == "awaiting_delivery"
    assert decision.reason == "quiet_deferred"


def test_execute_resolves_delivery_resolution() -> None:
    instance = _instance(decision="tell", level="critical")
    decision = resolve_trigger_due_decision(
        instance=instance, attention_mode="active", now=_NOW
    )
    assert decision.kind == "execute"
    assert decision.delivery_resolution is not None
    assert decision.delivery_resolution.presentation == "always"


def test_act_decision_is_not_blocked_by_attention() -> None:
    instance = TriggerInstance(
        id="trg-1",
        owner_id="owner-1",
        status="claimed",
        due_at=_NOW,
        created_at=_NOW,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=_NOW),
        action_snapshot=TriggerAction(decision="act", instructions="Archive mail"),
        attention_snapshot=AttentionPolicy(level="normal"),
        delivery_snapshot=DeliveryPlan(),
        freshness_snapshot=FreshnessPolicy(),
        management={"provider": "scheduler", "resource_id": "trg-1"},
    )
    decision = resolve_trigger_due_decision(
        instance=instance, attention_mode="paused", now=_NOW
    )
    assert decision.kind == "execute"
    assert decision.delivery_resolution is not None
    assert decision.delivery_resolution.presentation == "never"
