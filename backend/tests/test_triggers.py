"""Unit tests for core.triggers models, presets, service, and scheduler.

Run from backend/: pytest tests/test_triggers.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymongo.errors import DuplicateKeyError  # type: ignore[import-not-found]

from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    ManagementOwnership,
    TriggerAction,
    TriggerInstance,
    TriggerOrigin,
)
from core.triggers.presets import (
    alarm_preset,
    automation_preset,
    deferred_instruction_preset,
    reminder_preset,
    system_preset,
    timer_preset,
)
from core.triggers.service import TriggerService
from core.triggers.scheduler import TriggerScheduler


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


def test_instance_requires_explicit_freshness():
    now = datetime.now(timezone.utc)
    instance = TriggerInstance(
        id="trg-1",
        owner_id="geoff",
        status="pending",
        due_at=now,
        created_at=now,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=now),
        action_snapshot=TriggerAction(message="hey"),
        attention_snapshot=AttentionPolicy(),
        delivery_snapshot=DeliveryPlan(),
        freshness_snapshot=FreshnessPolicy(),
        management={"provider": "scheduler", "resource_id": "trg-1"},
    )
    assert instance.rule_id is None
    assert instance.dedup_key is None
    assert instance.turn_ids == []
    assert instance.freshness_snapshot == FreshnessPolicy()


def test_attention_policy_defaults():
    policy = AttentionPolicy()
    assert policy.level == "normal"
    assert policy.requires_ack is False
    assert policy.sound == "chime"


def test_delivery_plan_defaults():
    plan = DeliveryPlan()
    assert plan.channel == "voice"


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def test_reminder_preset_produces_tell_action():
    now = datetime.now(timezone.utc)
    kwargs = reminder_preset(owner_id="geoff", message="Call mum", fire_at=now)
    assert kwargs["action"].decision == "tell"
    assert kwargs["attention"].level == "normal"
    assert kwargs["attention"].sound == "chime"


def test_reminder_preset_maps_importance_to_sound():
    now = datetime.now(timezone.utc)
    urgent = reminder_preset(
        owner_id="geoff",
        message="Check oven",
        fire_at=now,
        importance="urgent",
    )
    assert urgent["attention"].sound == "timer"
    assert urgent["attention"].requires_ack is False


def test_reminder_preset_with_protocol_keeps_tell_decision():
    now = datetime.now(timezone.utc)
    kwargs = reminder_preset(
        owner_id="geoff",
        message="morning standup",
        fire_at=now,
        protocol_name="morning_brief",
    )
    assert kwargs["action"].decision == "tell"
    assert kwargs["action"].protocol_name == "morning_brief"


def test_reminder_preset_with_instructions_produces_offer_action():
    now = datetime.now(timezone.utc)
    kwargs = reminder_preset(
        owner_id="geoff",
        message="Move around",
        fire_at=now,
        recurrence="every 45m",
        instructions="only if I am not in a meeting",
        decision="offer",
    )
    assert kwargs["action"].decision == "offer"
    assert kwargs["action"].instructions == "only if I am not in a meeting"
    assert kwargs["attention"].sound == "chime"


def test_timer_preset_produces_urgent_timer_sound():
    kwargs = timer_preset(owner_id="geoff", message="Pizza done", duration_s=600)
    assert kwargs["attention"].level == "urgent"
    assert kwargs["attention"].sound == "timer"
    assert kwargs["origin"].kind == "interval"
    assert kwargs["origin"].duration_s == 600


def test_alarm_preset_produces_critical_with_ack():
    now = datetime.now(timezone.utc)
    kwargs = alarm_preset(owner_id="geoff", message="Wake up", fire_at=now)
    assert kwargs["attention"].level == "critical"
    assert kwargs["attention"].requires_ack is True
    assert kwargs["attention"].sound == "alarm"
    assert kwargs["freshness"].expire_after_due_s == 86400


def test_deferred_instruction_preset_uses_act_decision():
    now = datetime.now(timezone.utc)
    kwargs = deferred_instruction_preset(
        owner_id="geoff",
        instruction="Turn off the living room light.",
        fire_at=now,
    )
    assert kwargs["action"].decision == "act"
    assert kwargs["action"].instructions == "Turn off the living room light."
    assert kwargs["action"].reply_grounding == {}


def test_system_preset_defaults_to_offer():
    kwargs = system_preset(owner_id="geoff", message="disk full")
    assert kwargs["action"].decision == "offer"
    assert kwargs["attention"].level == "normal"


def test_automation_preset_external_trigger():
    kwargs = automation_preset(
        owner_id="geoff",
        message="New Slack mention",
        source="slack",
        event="new_message",
    )
    assert kwargs["origin"].kind == "external"
    assert kwargs["origin"].source == "slack"
    assert kwargs["freshness"] == FreshnessPolicy()


def test_calendar_automation_preset_expires_when_source_event_starts():
    kwargs = automation_preset(
        owner_id="geoff",
        message="Meeting starts soon",
        source="calendar",
        event="starting",
    )
    assert kwargs["freshness"].stale_if_source_event_started is True


# ---------------------------------------------------------------------------
# TriggerService
# ---------------------------------------------------------------------------


def _make_db(*, find_one=None, insert_one=None, update_one=None, find=None, count=None):
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value=find_one),
        insert_one=AsyncMock(return_value=insert_one),
        update_one=AsyncMock(return_value=update_one or SimpleNamespace(modified_count=1)),
        find=MagicMock(return_value=_ListCursor(find or [])),
        count_documents=AsyncMock(return_value=count or 0),
        find_one_and_update=AsyncMock(return_value=find_one),
    )
    return collection


class _ListCursor:
    def __init__(self, items):
        self._items = items

    async def to_list(self, _):
        return self._items


@pytest.mark.asyncio
async def test_create_instance_persists_to_mongo():
    now = datetime.now(timezone.utc)
    svc = TriggerService()

    collection = _make_db()
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        instance = await svc.create_instance(
            owner_id="geoff",
            origin=TriggerOrigin(kind="time", fire_at=now),
            action=TriggerAction(message="hey"),
            attention=AttentionPolicy(),
            delivery=DeliveryPlan(),
            freshness=FreshnessPolicy(),
        )

    assert instance.owner_id == "geoff"
    assert instance.status == "pending"
    collection.insert_one.assert_awaited_once()
    inserted = collection.insert_one.await_args.args[0]
    assert "dedup_key" not in inserted
    assert inserted["freshness_snapshot"] == {
        "stale_if_source_event_started": False,
        "on_expiry": "expire",
    }


@pytest.mark.asyncio
async def test_get_delivered_reply_grounding_projects_only_settled_instances():
    collection = _make_db(
        find=[
            {
                "id": "trg-sleep",
                "action_snapshot": {
                    "reply_grounding": {
                        "habit_name": "Consistent Sleep",
                        "checkin_kind": "habit_checkin",
                    }
                },
            }
        ]
    )
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        grounding = await TriggerService().get_delivered_reply_grounding(
            owner_id="geoff",
            instance_ids=["trg-sleep"],
        )

    assert grounding == {
        "trg-sleep": {
            "habit_name": "Consistent Sleep",
            "checkin_kind": "habit_checkin",
        }
    }
    query = collection.find.call_args.args[0]
    assert query["owner_id"] == "geoff"
    assert query["delivered_at"] == {"$exists": True, "$ne": None}


@pytest.mark.asyncio
async def test_create_instance_requires_management_for_linked_rule():
    with pytest.raises(ValueError, match="require management ownership"):
        await TriggerService().create_instance(
            owner_id="geoff",
            rule_id="rule-1",
            origin=TriggerOrigin(kind="time", fire_at=datetime.now(timezone.utc)),
            action=TriggerAction(message="hey"),
            attention=AttentionPolicy(),
            delivery=DeliveryPlan(),
            freshness=FreshnessPolicy(),
        )


@pytest.mark.asyncio
async def test_create_rule_persists_freshness_policy():
    now = datetime.now(timezone.utc)
    svc = TriggerService()
    collection = _make_db()
    fake_db = SimpleNamespace(trigger_rules=collection)
    freshness = FreshnessPolicy(expire_after_due_s=900)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        rule = await svc.create_rule(
            owner_id="geoff",
            name="Staleable reminder",
            origin=TriggerOrigin(kind="time", fire_at=now),
            action=TriggerAction(message="hey"),
            attention=AttentionPolicy(),
            delivery=DeliveryPlan(),
            freshness=freshness,
            management=ManagementOwnership(provider="scheduler"),
        )

    assert rule.freshness == freshness
    inserted = collection.insert_one.await_args.args[0]
    assert inserted["freshness"]["expire_after_due_s"] == 900


@pytest.mark.asyncio
async def test_create_instance_returns_existing_on_duplicate_dedup_key():
    now = datetime.now(timezone.utc)
    existing = {
        "id": "trg-existing",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": now,
        "created_at": now,
        "origin_snapshot": {"kind": "external"},
        "action_snapshot": {"decision": "tell", "message": "already fired"},
        "attention_snapshot": {"level": "normal", "sound": "chime"},
        "delivery_snapshot": {"channel": "voice"},
        "freshness_snapshot": {"stale_if_source_event_started": False},
        "dedup_key": "rule:item",
        "management": {"provider": "automations", "resource_id": "trg-existing"},
    }
    collection = _make_db(find_one=existing)
    collection.insert_one = AsyncMock(side_effect=DuplicateKeyError("duplicate"))
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        instance = await TriggerService().create_instance(
            owner_id="geoff",
            origin=TriggerOrigin(kind="external"),
            action=TriggerAction(message="already fired"),
            attention=AttentionPolicy(),
            delivery=DeliveryPlan(),
            freshness=FreshnessPolicy(),
            dedup_key="rule:item",
        )

    assert instance.id == "trg-existing"
    collection.find_one.assert_awaited_once_with({"dedup_key": "rule:item"})


@pytest.mark.asyncio
async def test_complete_instance_sets_completed_status():
    svc = TriggerService()
    collection = SimpleNamespace(update_one=AsyncMock())
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        await svc.complete_instance("trg-1", result_text="done")

    update_filter, update_op = collection.update_one.await_args.args
    assert update_filter == {"id": "trg-1"}
    assert update_op["$set"]["status"] == "completed"
    assert update_op["$set"]["result_text"] == "done"


@pytest.mark.asyncio
async def test_mark_delivered_sets_delivered_status():
    svc = TriggerService()
    collection = SimpleNamespace(
        update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)),
    )
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)), patch.object(
        svc, "supersede_siblings_after_settlement", AsyncMock()
    ):
        await svc.mark_delivered("trg-1", result_text="spoken")

    update_filter, update_op = collection.update_one.await_args.args
    assert update_filter == {"id": "trg-1", "status": {"$in": ["claimed", "executing"]}}
    assert update_op["$set"]["status"] == "delivered"
    assert "delivered_at" in update_op["$set"]
    assert "completed_at" not in update_op["$set"]
    assert update_op["$set"]["result_text"] == "spoken"
    assert update_op["$unset"] == {"failure_reason": "", "next_retry_at": ""}


@pytest.mark.asyncio
async def test_mark_awaiting_delivery():
    svc = TriggerService()
    collection = SimpleNamespace(update_one=AsyncMock())
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        await svc.mark_awaiting_delivery("trg-2", reason="offline")

    update_filter, update_op = collection.update_one.await_args.args
    assert update_filter == {"id": "trg-2", "status": {"$in": ["claimed", "executing"]}}
    assert update_op["$set"]["status"] == "awaiting_delivery"
    assert update_op["$set"]["failure_reason"] == "offline"


@pytest.mark.asyncio
async def test_mark_awaiting_delivery_with_retry_time_sets_next_retry_at():
    svc = TriggerService()
    collection = SimpleNamespace(update_one=AsyncMock())
    fake_db = SimpleNamespace(trigger_instances=collection)
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        await svc.mark_awaiting_delivery("trg-2", reason="offer_deferred", next_retry_at=retry_at)

    _, update_op = collection.update_one.await_args.args
    assert update_op["$set"]["next_retry_at"] == retry_at
    assert "$inc" not in update_op


@pytest.mark.asyncio
async def test_get_awaiting_delivery_can_filter_retry_due():
    svc = TriggerService()
    collection = _make_db(find=[])
    fake_db = SimpleNamespace(trigger_instances=collection)
    now = datetime.now(timezone.utc)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        await svc.get_awaiting_delivery("geoff", retry_due_at=now, include_unscheduled=False)

    query = collection.find.call_args.args[0]
    assert query == {
        "owner_id": "geoff",
        "status": "awaiting_delivery",
        "$or": [{"next_retry_at": {"$lte": now}}],
    }


@pytest.mark.asyncio
async def test_claim_awaiting_instance_moves_back_to_claimed():
    svc = TriggerService()
    collection = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        claimed = await svc.claim_awaiting_instance("trg-2")

    assert claimed is True
    update_filter, update_op = collection.update_one.await_args.args
    assert update_filter == {"id": "trg-2", "status": "awaiting_delivery"}
    assert update_op["$set"]["status"] == "claimed"
    assert update_op["$unset"] == {"next_retry_at": "", "failure_reason": ""}


@pytest.mark.asyncio
async def test_suppress_instance_stores_reason():
    svc = TriggerService()
    collection = SimpleNamespace(update_one=AsyncMock())
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        await svc.suppress_instance("trg-2", reason="offer_no_reply")

    update_filter, update_op = collection.update_one.await_args.args
    assert update_filter == {"id": "trg-2"}
    assert update_op["$set"]["status"] == "suppressed"
    assert update_op["$set"]["failure_reason"] == "offer_no_reply"


@pytest.mark.asyncio
async def test_record_turn_id_adds_attempt_to_instance():
    svc = TriggerService()
    collection = SimpleNamespace(update_one=AsyncMock())
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        await svc.record_turn_id("trg-2", "turn-1")

    update_filter, update_op = collection.update_one.await_args.args
    assert update_filter == {"id": "trg-2"}
    assert update_op["$addToSet"] == {"turn_ids": "turn-1"}
    assert "updated_at" in update_op["$set"]


@pytest.mark.asyncio
async def test_expire_instance_sets_expired_status():
    svc = TriggerService()
    collection = SimpleNamespace(update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)))
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        expired = await svc.expire_instance("trg-2", reason="delivery_ttl_expired")

    assert expired is True
    update_filter, update_op = collection.update_one.await_args.args
    assert update_filter == {
        "id": "trg-2",
        "status": {"$in": ["pending", "claimed", "executing", "awaiting_delivery"]},
    }
    assert update_op["$set"]["status"] == "expired"
    assert update_op["$set"]["failure_reason"] == "delivery_ttl_expired"


@pytest.mark.asyncio
async def test_snooze_instance_uses_original_owner_and_creates_child():
    now = datetime.now(timezone.utc)
    original = {
        "id": "trg-1",
        "rule_id": "rule-wake",
        "owner_id": "geoff",
        "status": "delivered",
        "due_at": now,
        "created_at": now,
        "origin_snapshot": {"kind": "time", "fire_at": now},
        "action_snapshot": {"decision": "tell", "message": "Wake up"},
        "attention_snapshot": {"level": "critical", "requires_ack": True, "sound": "alarm"},
        "delivery_snapshot": {"channel": "voice"},
        "freshness_snapshot": {"stale_if_source_event_started": False},
        "source_event": {"source": "test"},
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
    }
    collection = _make_db(find_one=original)
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        child = await TriggerService().snooze_instance(
            "trg-1",
            snooze_until=now + timedelta(minutes=10),
        )

    assert child is not None
    assert child.owner_id == "geoff"
    assert child.rule_id is None
    assert child.source_event["snoozed_from"] == "trg-1"
    update_filter, update_op = collection.find_one_and_update.await_args.args
    assert update_filter["id"] == "trg-1"
    assert update_filter["status"]["$in"] == ["claimed", "executing", "delivered", "awaiting_delivery"]
    assert update_filter["$or"] == [
        {"attention_snapshot.requires_ack": True},
        {"attention_snapshot.sound": {"$in": ["alarm", "timer"]}},
    ]
    assert update_op["$set"]["status"] == "snoozed"


@pytest.mark.asyncio
async def test_snooze_instance_ignores_non_ackable_trigger():
    collection = _make_db(find_one=None)
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        child = await TriggerService().snooze_instance(
            "trg-normal",
            snooze_until=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

    assert child is None
    collection.insert_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_acknowledge_latest_for_owner_finds_ackable_trigger():
    now = datetime.now(timezone.utc)
    doc = {
        "id": "trg-2",
        "owner_id": "geoff",
        "status": "delivered",
        "due_at": now,
        "created_at": now,
        "origin_snapshot": {"kind": "time", "fire_at": now},
        "action_snapshot": {"decision": "tell", "message": "Wake up"},
        "attention_snapshot": {"level": "critical", "requires_ack": True, "sound": "alarm"},
        "delivery_snapshot": {"channel": "voice"},
    }
    collection = _make_db(find_one=doc)
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)), patch.object(
        TriggerService, "supersede_siblings_after_settlement", AsyncMock()
    ):
        acknowledged = await TriggerService().acknowledge_latest_for_owner("geoff")

    assert acknowledged is not None
    assert acknowledged["id"] == "trg-2"
    find_filter = collection.find_one.await_args_list[0].args[0]
    assert find_filter["owner_id"] == "geoff"
    assert find_filter["status"]["$in"] == ["claimed", "executing", "delivered", "awaiting_delivery"]
    update_filter, update_op = collection.find_one_and_update.await_args.args
    assert update_filter["id"] == "trg-2"
    assert update_filter["status"]["$in"] == ["claimed", "executing", "delivered", "awaiting_delivery"]
    assert update_op["$set"]["status"] == "acknowledged"
    assert "acknowledged_at" in update_op["$set"]


@pytest.mark.asyncio
async def test_acknowledge_instance_ignores_non_ackable_trigger():
    svc = TriggerService()
    collection = SimpleNamespace(
        find_one_and_update=AsyncMock(return_value=None),
    )
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.service.mongodb", SimpleNamespace(db=fake_db)):
        acknowledged = await svc.acknowledge_instance("trg-normal")

    assert acknowledged is False
    update_filter, _ = collection.find_one_and_update.await_args.args
    assert update_filter["id"] == "trg-normal"
    assert update_filter["$or"] == [
        {"attention_snapshot.requires_ack": True},
        {"attention_snapshot.sound": {"$in": ["alarm", "timer"]}},
    ]


# ---------------------------------------------------------------------------
# TriggerScheduler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_orphans_resets_claimed_and_executing():
    scheduler = TriggerScheduler()
    collection = SimpleNamespace(
        update_many=AsyncMock(return_value=SimpleNamespace(modified_count=3))
    )
    fake_db = SimpleNamespace(trigger_instances=collection)

    with patch("core.triggers.scheduler.mongodb", SimpleNamespace(db=fake_db)):
        await scheduler._recover_orphans()

    filter_arg, update_arg = collection.update_many.await_args.args
    assert filter_arg["status"]["$in"] == ["claimed", "executing"]
    assert update_arg["$set"]["status"] == "awaiting_delivery"


@pytest.mark.asyncio
async def test_process_due_publishes_trigger_due_event():
    scheduler = TriggerScheduler()
    now = datetime.now(timezone.utc)
    instance_doc = {
        "id": "trg-1",
        "owner_id": "geoff",
        "status": "claimed",
        "due_at": now,
        "rule_id": None,
        "origin_snapshot": {"kind": "time"},
        "action_snapshot": {"decision": "tell", "message": "hi"},
        "attention_snapshot": {"level": "normal"},
        "delivery_snapshot": {"channel": "voice"},
    }

    pending_doc = {**instance_doc, "status": "pending"}
    collection = SimpleNamespace(
        distinct=AsyncMock(return_value=[]),
        find_one=AsyncMock(side_effect=[pending_doc, None]),
        find_one_and_update=AsyncMock(return_value=instance_doc),
    )
    fake_db = SimpleNamespace(trigger_instances=collection)

    published_events = []

    class _FakeBus:
        async def publish(self, event):
            published_events.append(event)

    with (
        patch("core.triggers.scheduler.mongodb", SimpleNamespace(db=fake_db)),
        patch("core.triggers.scheduler.event_bus", _FakeBus()),
    ):
        await scheduler._process_due()

    assert len(published_events) == 1
    assert published_events[0].data["instance_id"] == "trg-1"
