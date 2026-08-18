"""Explicit freshness for awaiting_delivery trigger replay."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    TriggerAction,
    TriggerInstance,
    TriggerOrigin,
)
from core.triggers.freshness import freshness_forces_delivery, trigger_expiry_reason
from core.triggers.presets import ALARM_EXPIRE_AFTER_DUE_S


def _instance(
    *,
    sound: str = "chime",
    requires_ack: bool = False,
    minutes_ago: int = 0,
    freshness: FreshnessPolicy | None = None,
) -> TriggerInstance:
    now = datetime.now(timezone.utc)
    due_at = now - timedelta(minutes=minutes_ago)
    return TriggerInstance(
        id="inst-1",
        rule_id=None,
        owner_id="home",
        status="awaiting_delivery",
        due_at=due_at,
        created_at=due_at,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=due_at),
        action_snapshot=TriggerAction(decision="tell", message="Reminder"),
        attention_snapshot=AttentionPolicy(sound=sound, requires_ack=requires_ack),
        delivery_snapshot=DeliveryPlan(),
        freshness_snapshot=freshness or FreshnessPolicy(),
        management={"provider": "scheduler", "resource_id": "inst-1"},
    )


def test_alarm_preset_ttl_expires_after_one_day():
    assert trigger_expiry_reason(
        _instance(
            sound="alarm",
            requires_ack=True,
            minutes_ago=int(ALARM_EXPIRE_AFTER_DUE_S / 60) + 5,
            freshness=FreshnessPolicy(expire_after_due_s=ALARM_EXPIRE_AFTER_DUE_S),
        )
    ) == "delivery_ttl_expired"


def test_replay_expires_when_explicit_deadline_passed():
    assert trigger_expiry_reason(
        _instance(minutes_ago=20, freshness=FreshnessPolicy(expire_after_due_s=600))
    ) == "delivery_ttl_expired"


def test_force_deliver_freshness_still_reports_expiry_reason():
    instance = _instance(
        minutes_ago=20,
        freshness=FreshnessPolicy(expire_after_due_s=600, on_expiry="force_deliver"),
    )
    reason = trigger_expiry_reason(instance)

    assert reason == "delivery_ttl_expired"
    assert freshness_forces_delivery(instance, reason) is True


def test_calendar_event_started_is_not_force_delivered():
    now = datetime.now(timezone.utc)
    instance = _instance(
        freshness=FreshnessPolicy(
            stale_if_source_event_started=True,
            on_expiry="force_deliver",
        ),
    )
    instance.origin_snapshot.source = "calendar"
    instance.origin_snapshot.offset_minutes = -1
    instance.source_event = {"item": {"start": (now - timedelta(seconds=1)).isoformat()}}

    reason = trigger_expiry_reason(instance, now=now)

    assert reason == "calendar_event_started"
    assert freshness_forces_delivery(instance, reason) is False


def test_replay_uses_source_event_staleness_policy():
    now = datetime.now(timezone.utc)
    instance = _instance(freshness=FreshnessPolicy(stale_if_source_event_started=True))
    instance.origin_snapshot.source = "calendar"
    instance.source_event = {"item": {"start": (now - timedelta(minutes=5)).isoformat()}}

    assert trigger_expiry_reason(instance, now=now) == "calendar_event_started"
