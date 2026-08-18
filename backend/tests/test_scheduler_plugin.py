from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import datetime as dt_stdlib

from core.triggers.delivery_policy import with_target_fallback_for_critical
from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    DeliveryTargetHint,
    ManagementOwnership,
)
from core.triggers.scheduler import TriggerScheduler
from plugins.scheduler import SchedulerPlugin


def _text(result) -> str:
    if hasattr(result, "content") and not hasattr(result, "code"):
        return result.content
    if hasattr(result, "message"):
        return result.message
    return str(result)


def _ui(result) -> list:
    return list(getattr(result, "ui", None) or [])


@pytest.mark.asyncio
async def test_get_alerts_empty_result_is_complete_evidence(monkeypatch) -> None:
    class Cursor:
        async def to_list(self, _):
            return []

    db = SimpleNamespace(
        trigger_instances=SimpleNamespace(find=MagicMock(return_value=Cursor())),
        trigger_rules=SimpleNamespace(find=MagicMock(return_value=Cursor())),
    )
    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.mongodb", SimpleNamespace(db=db))

    result = await SchedulerPlugin().get_alerts(kind="alarm", query="wake")

    assert result.alerts == []
    assert result.match_status == "none"
    assert result.coverage == "complete"
    assert result.kind == "alarm"
    assert result.query == "wake"


@pytest.fixture(autouse=True)
def _treat_linked_rules_as_scheduler_managed(monkeypatch):
    async def _passthrough(_owner_id: str, rule_ids: set[str]) -> set[str]:
        return rule_ids

    monkeypatch.setattr("plugins.scheduler._scheduler_rule_ids", _passthrough)


def test_with_target_fallback_for_critical_only_for_pinned_critical_targets():
    delivery = DeliveryPlan(
        target=DeliveryTargetHint(location_ref={"room_id": "bedroom"}),
    )
    alarm_attention = AttentionPolicy(level="critical", sound="alarm", requires_ack=True)
    reminder_attention = AttentionPolicy(level="normal", sound="chime")

    fallback = with_target_fallback_for_critical(delivery, alarm_attention)
    assert fallback.fallback == "follow_me_if_target_unavailable"

    unchanged = with_target_fallback_for_critical(delivery, reminder_attention)
    assert unchanged.fallback == "none"

    anywhere = with_target_fallback_for_critical(DeliveryPlan(), alarm_attention)
    assert anywhere.fallback == "none"


@pytest.mark.asyncio
async def test_get_alerts_includes_seconds_remaining(monkeypatch) -> None:
    fixed_now = datetime(2026, 5, 10, 10, 0, 0, tzinfo=timezone.utc)
    due = datetime(2026, 5, 10, 10, 0, 45, tzinfo=timezone.utc)

    class FrozenDatetime:
        def __init__(self, frozen: datetime) -> None:
            self._frozen = frozen

        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return self._frozen

    doc = {
        "id": "trg-x",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": due,
        "action_snapshot": {"message": "ping"},
        "attention_snapshot": {"sound": "timer"},
        "origin_snapshot": {"kind": "interval"},
        "delivery_snapshot": {
            "target": {"node_id": "kitchen-sat"},
        },
        "management": {"provider": "scheduler", "resource_id": "trg-x"},
    }

    class Cursor:
        def __init__(self, items):
            self._items = items

        async def to_list(self, _):
            return self._items

    find_mock = MagicMock(return_value=Cursor([doc]))
    db = SimpleNamespace(
        trigger_instances=SimpleNamespace(find=find_mock),
        trigger_rules=SimpleNamespace(find=MagicMock(return_value=Cursor([]))),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime(fixed_now))

    result = await SchedulerPlugin().get_alerts()
    rows = result.alerts
    assert result.match_status == "single"
    assert result.coverage == "complete"
    assert len(rows) == 1
    row = rows[0]
    assert row.seconds_remaining == 45
    assert row.minutes_remaining == 0
    assert row.instance_id == "trg-x"
    assert row.series_id is None
    assert row.scope == "instance"
    assert row.delivery_target == "here"
    assert row.delivery_target_kind == "node"
    dumped = row.model_dump(exclude_none=True)
    assert "delivery_target_ref" not in dumped
    assert "type" not in dumped
    assert "timezone" not in dumped
    assert row.kind == "timer"
    assert row.origin_kind == "interval"
    assert row.requires_ack is False


@pytest.mark.asyncio
async def test_get_alerts_returns_local_time_for_scheduled_reminder(monkeypatch) -> None:
    fixed_now = datetime(2026, 5, 25, 15, 28, 0, tzinfo=timezone.utc)
    due = "2026-05-26T13:00:00Z"

    class FrozenDatetime:
        def __init__(self, frozen: datetime) -> None:
            self._frozen = frozen

        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return self._frozen

    doc = {
        "id": "trg-sleep",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": due,
        "rule_id": "rule-sleep",
        "action_snapshot": {"message": "Time to wind down and get off screens."},
        "attention_snapshot": {"sound": "chime"},
        "origin_snapshot": {
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "23:00",
        },
    }

    class Cursor:
        def __init__(self, items):
            self._items = items

        async def to_list(self, _):
            return self._items

    db = SimpleNamespace(
        trigger_instances=SimpleNamespace(find=MagicMock(return_value=Cursor([doc]))),
        trigger_rules=SimpleNamespace(find=MagicMock(return_value=Cursor([]))),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime(fixed_now))

    rows = (await SchedulerPlugin().get_alerts()).alerts

    assert rows[0].time == "2026-05-26T23:00:00+10:00"
    assert rows[0].local_time == "11:00 PM"
    assert rows[0].scheduled_local_time == "23:00"
    assert rows[0].local_date == "2026-05-26"
    dumped = rows[0].model_dump(exclude_none=True)
    assert "utc_time" not in dumped
    assert "timezone" not in dumped


@pytest.mark.asyncio
async def test_get_alerts_filters_daily_alarms_by_local_time(monkeypatch) -> None:
    fixed_now = datetime(2026, 6, 23, 15, 30, 0, tzinfo=timezone.utc)
    due = datetime(2026, 6, 23, 23, 30, 0, tzinfo=timezone.utc)

    class FrozenDatetime:
        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return fixed_now

        def strptime(self, *args, **kwargs):
            return dt_stdlib.datetime.strptime(*args, **kwargs)

    reminder_doc = {
        "id": "trg-reminder",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": due,
        "action_snapshot": {"message": "Wake up"},
        "attention_snapshot": {"sound": "chime", "requires_ack": False},
        "origin_snapshot": {"kind": "time", "timezone": "Australia/Sydney"},
        "delivery_snapshot": {},
        "management": {"provider": "scheduler", "resource_id": "trg-reminder"},
    }
    alarm_doc = {
        "id": "trg-alarm",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": due,
        "rule_id": "rule-wake",
        "action_snapshot": {"message": "Wake up"},
        "attention_snapshot": {"sound": "alarm", "requires_ack": True},
        "origin_snapshot": {
            "kind": "time",
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "09:30",
        },
        "delivery_snapshot": {},
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
    }

    class Cursor:
        def __init__(self, items):
            self._items = items

        async def to_list(self, _):
            return self._items

    db = SimpleNamespace(
        trigger_instances=SimpleNamespace(
            find=MagicMock(return_value=Cursor([reminder_doc, alarm_doc])),
        ),
        trigger_rules=SimpleNamespace(find=MagicMock(return_value=Cursor([]))),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime())

    result = await SchedulerPlugin().get_alerts(
        kind="alarm",
        recurrence="daily",
        local_time="9:30",
    )
    rows = result.alerts

    assert len(rows) == 1
    assert rows[0].instance_id == "trg-alarm"
    assert rows[0].kind == "alarm"
    assert rows[0].recurrence == "daily"


@pytest.mark.asyncio
async def test_get_alerts_includes_silent_work_and_filters_by_content(monkeypatch) -> None:
    fixed_now = datetime(2026, 6, 23, 15, 30, 0, tzinfo=timezone.utc)
    due = datetime(2026, 6, 23, 22, 30, 0, tzinfo=timezone.utc)

    class FrozenDatetime:
        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return fixed_now

    pending_alarm = {
        "id": "trg-alarm",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": due,
        "action_snapshot": {"decision": "tell", "message": "Wake up"},
        "attention_snapshot": {"sound": "alarm", "requires_ack": True},
        "origin_snapshot": {"kind": "time", "timezone": "Australia/Sydney"},
        "delivery_snapshot": {},
        "management": {"provider": "scheduler", "resource_id": "trg-alarm"},
    }
    awaiting_alarm = {
        **pending_alarm,
        "id": "trg-awaiting",
        "status": "awaiting_delivery",
    }
    deferred_work = {
        "id": "trg-defer",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": due,
        "action_snapshot": {
            "decision": "act",
            "message": "Turn off lights",
            "instructions": "Turn off all lights in the house",
        },
        "attention_snapshot": {"sound": "none", "requires_ack": False},
        "origin_snapshot": {"kind": "time", "timezone": "Australia/Sydney"},
        "delivery_snapshot": {},
        "management": {"provider": "scheduler", "resource_id": "trg-defer"},
    }

    class Cursor:
        def __init__(self, items):
            self._items = items

        async def to_list(self, _):
            return self._items

    db = SimpleNamespace(
        trigger_instances=SimpleNamespace(
            find=MagicMock(return_value=Cursor([pending_alarm, awaiting_alarm, deferred_work])),
        ),
        trigger_rules=SimpleNamespace(find=MagicMock(return_value=Cursor([]))),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime())

    rows = (await SchedulerPlugin().get_alerts(status="pending")).alerts

    assert [row.instance_id for row in rows] == ["trg-alarm", "trg-defer"]
    assert rows[0].kind == "alarm"
    assert rows[1].kind is None

    by_message = (await SchedulerPlugin().get_alerts(message="lights")).alerts
    assert len(by_message) == 1
    assert by_message[0].instance_id == "trg-defer"

    by_query = (await SchedulerPlugin().get_alerts(query="lights")).alerts
    assert len(by_query) == 1
    assert by_query[0].instance_id == "trg-defer"

    by_instruction = (await SchedulerPlugin().get_alerts(
        message="all lights in the house",
    )).alerts
    assert len(by_instruction) == 1
    assert by_instruction[0].instance_id == "trg-defer"


@pytest.mark.asyncio
async def test_get_alerts_defaults_to_pending_inventory_for_kind_filter(monkeypatch) -> None:
    fixed_now = datetime(2026, 6, 23, 15, 30, 0, tzinfo=timezone.utc)
    due = datetime(2026, 6, 23, 22, 30, 0, tzinfo=timezone.utc)

    class FrozenDatetime:
        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return fixed_now

    pending_alarm = {
        "id": "trg-alarm",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": due,
        "rule_id": "rule-wake",
        "action_snapshot": {"decision": "tell", "message": "Wake up"},
        "attention_snapshot": {"sound": "alarm", "requires_ack": True},
        "origin_snapshot": {
            "kind": "time",
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:30",
        },
        "delivery_snapshot": {},
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
    }

    class Cursor:
        async def to_list(self, _):
            return [pending_alarm]

    find = MagicMock(return_value=Cursor())
    db = SimpleNamespace(
        trigger_instances=SimpleNamespace(find=find),
        trigger_rules=SimpleNamespace(find=MagicMock(return_value=Cursor())),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime())

    rows = (await SchedulerPlugin().get_alerts(kind="alarm")).alerts

    find.assert_called_once_with({
        "owner_id": "geoff",
        "status": {"$in": ["pending"]},
    })
    assert [row.instance_id for row in rows] == ["trg-alarm"]
    assert rows[0].kind == "alarm"


@pytest.mark.asyncio
async def test_get_next_alert_returns_next_pending_instance(monkeypatch) -> None:
    fixed_now = datetime(2026, 6, 23, 15, 30, 0, tzinfo=timezone.utc)
    stale_due = datetime(2026, 6, 22, 22, 30, 0, tzinfo=timezone.utc)
    next_due = datetime(2026, 6, 23, 22, 30, 0, tzinfo=timezone.utc)

    class FrozenDatetime:
        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return fixed_now

    stale_doc = {
        "id": "trg-stale",
        "owner_id": "geoff",
        "status": "awaiting_delivery",
        "due_at": stale_due,
        "rule_id": "rule-wake",
        "action_snapshot": {"message": "Wake up"},
        "attention_snapshot": {"sound": "alarm", "requires_ack": True},
        "origin_snapshot": {
            "kind": "time",
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:30",
        },
        "delivery_snapshot": {},
    }
    pending_doc = {
        "id": "trg-next",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": next_due,
        "rule_id": "rule-wake",
        "action_snapshot": {"message": "Wake up"},
        "attention_snapshot": {"sound": "alarm", "requires_ack": True},
        "origin_snapshot": {
            "kind": "time",
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:30",
        },
        "delivery_snapshot": {},
    }

    class Cursor:
        def __init__(self, items):
            self._items = items

        async def to_list(self, _):
            return self._items

    db = SimpleNamespace(
        trigger_instances=SimpleNamespace(
            find=MagicMock(return_value=Cursor([stale_doc, pending_doc])),
        ),
        trigger_rules=SimpleNamespace(find=MagicMock(return_value=Cursor([]))),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime())

    row = await SchedulerPlugin().get_next_alert(kind="alarm")

    assert not isinstance(row, str)
    assert row.instance_id == "trg-next"
    assert row.status == "pending"
    assert row.kind == "alarm"


@pytest.mark.asyncio
async def test_get_next_alert_excludes_silent_work(monkeypatch) -> None:
    fixed_now = datetime(2026, 6, 23, 15, 30, 0, tzinfo=timezone.utc)
    deferred_due = datetime(2026, 6, 23, 18, 0, 0, tzinfo=timezone.utc)
    alarm_due = datetime(2026, 6, 23, 22, 30, 0, tzinfo=timezone.utc)

    class FrozenDatetime:
        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return fixed_now

    deferred_doc = {
        "id": "trg-defer",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": deferred_due,
        "action_snapshot": {"decision": "act", "message": "Turn off lights"},
        "attention_snapshot": {"sound": "none", "requires_ack": False},
        "origin_snapshot": {"kind": "time", "timezone": "Australia/Sydney"},
        "delivery_snapshot": {},
        "management": {"provider": "scheduler", "resource_id": "trg-defer"},
    }
    alarm_doc = {
        "id": "trg-alarm",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": alarm_due,
        "action_snapshot": {"decision": "tell", "message": "Wake up"},
        "attention_snapshot": {"sound": "alarm", "requires_ack": True},
        "origin_snapshot": {"kind": "time", "timezone": "Australia/Sydney"},
        "delivery_snapshot": {},
        "management": {"provider": "scheduler", "resource_id": "trg-alarm"},
    }

    class Cursor:
        def __init__(self, items):
            self._items = items

        async def to_list(self, _):
            return self._items

    db = SimpleNamespace(
        trigger_instances=SimpleNamespace(
            find=MagicMock(return_value=Cursor([deferred_doc, alarm_doc])),
        ),
        trigger_rules=SimpleNamespace(find=MagicMock(return_value=Cursor([]))),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime())

    row = await SchedulerPlugin().get_next_alert()

    assert not isinstance(row, str)
    assert row.instance_id == "trg-alarm"
    assert row.kind == "alarm"


@pytest.mark.asyncio
async def test_get_alerts_surfaces_recurring_series_without_pending_instance(monkeypatch) -> None:
    now = datetime(2026, 6, 4, 0, 0, 0, tzinfo=timezone.utc)
    due = datetime(2026, 6, 4, 0, 30, 0, tzinfo=timezone.utc)

    class FrozenDatetime:
        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return now

    instance_doc = {
        "id": "trg-live",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": due,
        "rule_id": "rule-live",
        "action_snapshot": {"message": "Stand up"},
        "attention_snapshot": {"sound": "chime"},
        "origin_snapshot": {"recurrence": "every 30m", "timezone": "UTC"},
        "delivery_snapshot": {},
        "management": {"provider": "scheduler", "resource_id": "rule-live"},
    }
    rules = [
        {
            "id": "rule-live",
            "name": "Morning Wakeup Lights",
            "enabled": True,
            "surface": True,
            "origin": {"kind": "time", "recurrence": "every 30m"},
            "action": {"instructions": "Fade in the bedroom lights"},
            "attention": {"sound": "chime"},
            "delivery": {},
            "management": {"provider": "scheduler", "resource_id": "rule-live"},
        },
        {
            "id": "rule-orphan",
            "name": "Pushups",
            "enabled": True,
            "surface": True,
            "origin": {"kind": "time", "recurrence": "every 30m"},
            "action": {"message": "Time for pushups"},
            "attention": {"sound": "chime"},
            "delivery": {
                "target": {
                    "location_ref": {
                        "room_id": "bedroom",
                        "room_name": "Bedroom",
                        "ha_area_id": "area-bedroom",
                    },
                },
            },
            "management": {"provider": "scheduler", "resource_id": "rule-orphan"},
        },
    ]

    class Cursor:
        def __init__(self, items):
            self._items = items

        async def to_list(self, *_args, **_kwargs):
            return self._items

    db = SimpleNamespace(
        trigger_instances=SimpleNamespace(find=MagicMock(return_value=Cursor([instance_doc]))),
        trigger_rules=SimpleNamespace(find=MagicMock(return_value=Cursor(rules))),
    )
    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr("plugins.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime())

    rows = (await SchedulerPlugin().get_alerts()).alerts

    by_rule = {row.series_id: row for row in rows}
    assert set(by_rule) == {"rule-live", "rule-orphan"}
    # The live series is represented by its pending instance (has a time).
    assert by_rule["rule-live"].instance_id == "trg-live"
    assert by_rule["rule-live"].name == "Morning Wakeup Lights"
    assert by_rule["rule-live"].time is not None
    # The orphaned series surfaces from the rule with no upcoming time.
    assert by_rule["rule-orphan"].status == "active"
    assert by_rule["rule-orphan"].time is None
    assert by_rule["rule-orphan"].message == "Time for pushups"
    assert by_rule["rule-orphan"].delivery_target == "Bedroom"
    assert by_rule["rule-orphan"].delivery_target_kind == "room"

    matching = (await SchedulerPlugin().get_alerts(query="morning")).alerts
    assert [row.series_id for row in matching] == ["rule-live"]


@pytest.mark.asyncio
async def test_remind_with_instructions_creates_offer_trigger(monkeypatch) -> None:
    trigger_time = datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc)
    created = SimpleNamespace(id="trg-1")
    create_instance = AsyncMock(return_value=created)

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: trigger_time)
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_instance=create_instance),
    )

    result = await SchedulerPlugin().remind(
        when="tomorrow 10:00",
        message="Morning briefing",
        instructions="Check today's calendar and current to-do list, then brief me.",
        decision="offer",
    )

    assert "Reminder set" in _text(result)
    kwargs = create_instance.await_args.kwargs
    assert kwargs["action"].decision == "offer"
    assert kwargs["action"].message == "Morning briefing"
    assert kwargs["action"].instructions == "Check today's calendar and current to-do list, then brief me."


@pytest.mark.asyncio
async def test_defer_creates_act_trigger(monkeypatch) -> None:
    trigger_time = datetime(2026, 5, 8, 0, 5, tzinfo=timezone.utc)
    created = SimpleNamespace(id="trg-1")
    create_instance = AsyncMock(return_value=created)

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: trigger_time)
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_instance=create_instance),
    )

    result = await SchedulerPlugin().defer(
        when="5m",
        instruction="Turn off the living room light.",
    )

    assert _text(result).startswith("Deferred instruction scheduled")
    kwargs = create_instance.await_args.kwargs
    assert kwargs["due_at"] == trigger_time
    assert kwargs["origin"].kind == "time"
    assert kwargs["origin"].fire_at == trigger_time
    assert kwargs["action"].decision == "act"
    assert kwargs["action"].message == "Turn off the living room light."
    assert kwargs["action"].instructions == "Turn off the living room light."
    assert kwargs["action"].reply_grounding == {}
    assert kwargs["attention"].level == "normal"
    assert kwargs["attention"].sound == "none"
    assert kwargs["delivery"].channel == "voice"


@pytest.mark.asyncio
async def test_remind_pushes_receipt_for_one_off_reminder(monkeypatch) -> None:
    trigger_time = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    created = SimpleNamespace(id="trg-1")

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: trigger_time)
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_instance=AsyncMock(return_value=created)),
    )

    result = await SchedulerPlugin().remind(
        when="tomorrow 10:00",
        message="Take out the bins",
    )

    assert "Reminder set" in _text(result)
    assert _ui(result)[0].component == "ContentWidget"
    assert _ui(result)[0].data["display"] == "receipt"
    assert _ui(result)[0].data["line"] == "Take out the bins"


@pytest.mark.asyncio
async def test_recurring_remind_previews_before_persisting(monkeypatch) -> None:
    trigger_time = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    create_rule = AsyncMock()
    create_instance = AsyncMock()

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: trigger_time)
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_rule=create_rule, create_instance=create_instance),
    )

    result = await SchedulerPlugin().remind(
        when="tomorrow 10:00",
        message="Morning briefing",
        recurrence="daily",
    )

    assert "Preview only" in _text(result)
    assert _ui(result)[0].component == "ContentWidget"
    assert _ui(result)[0].data["title"] == "Recurring Reminder Preview"
    create_rule.assert_not_awaited()
    create_instance.assert_not_awaited()


@pytest.mark.asyncio
async def test_recurring_remind_confirmed_persists_rule_and_instance(monkeypatch) -> None:
    trigger_time = datetime(2026, 5, 8, 13, 0, tzinfo=timezone.utc)
    rule = SimpleNamespace(
        id="rule-1",
        management=ManagementOwnership(provider="scheduler", resource_id="rule-1"),
    )
    instance = SimpleNamespace(id="trg-1")
    create_rule = AsyncMock(return_value=rule)
    create_instance = AsyncMock(return_value=instance)

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "Australia/Sydney")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: trigger_time)
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_rule=create_rule, create_instance=create_instance),
    )

    result = await SchedulerPlugin().remind(
        when="tomorrow 10:00",
        message="Morning briefing",
        recurrence="daily",
        confirmed=True,
    )

    assert "Series ID: rule-1" in _text(result)
    create_rule.assert_awaited_once()
    create_instance.assert_awaited_once()
    assert create_rule.await_args.kwargs["origin"].original_local_time == "23:00"
    assert create_instance.await_args.kwargs["origin"].original_local_time == "23:00"


@pytest.mark.asyncio
async def test_remind_rejects_missing_protocol_before_persisting(monkeypatch) -> None:
    create_instance = AsyncMock()

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.protocol.protocol_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_instance=create_instance),
    )

    result = await SchedulerPlugin().remind(
        when="tomorrow 10:00",
        message="Morning briefing",
        protocol="missing_briefing",
    )

    assert "Protocol 'missing_briefing' not found" in _text(result)
    assert "instructions" in _text(result)
    create_instance.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_alert_deletes_recurring_rule_definition(monkeypatch) -> None:
    trigger_rules = SimpleNamespace(
        find_one=AsyncMock(return_value={
            "id": "rule-1",
            "owner_id": "geoff",
            "origin": {"kind": "time"},
            "surface": True,
            "management": {"provider": "scheduler", "resource_id": "rule-1"},
        }),
        delete_one=AsyncMock(),
    )
    trigger_instances = SimpleNamespace(update_many=AsyncMock())
    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr(
        "plugins.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_rules=trigger_rules,
            trigger_instances=trigger_instances,
        )),
    )

    result = await SchedulerPlugin().cancel_alert(series_id="rule-1")

    assert _text(result) == "Recurring series cancelled."
    trigger_rules.delete_one.assert_awaited_once_with({"id": "rule-1", "owner_id": "geoff"})
    trigger_instances.update_many.assert_awaited_once()
    query, update = trigger_instances.update_many.await_args.args
    assert query == {
        "rule_id": "rule-1",
        "owner_id": "geoff",
        "status": {"$in": ["pending", "awaiting_delivery"]},
    }
    assert update["$set"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_replace_alert_updates_one_shot_in_place(monkeypatch) -> None:
    original_due = datetime(2026, 6, 4, 13, 27, 0, tzinfo=timezone.utc)
    original = {
        "id": "trg-old",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": original_due,
        "origin_snapshot": {
            "kind": "time",
            "fire_at": original_due,
            "timezone": "Australia/Sydney",
        },
        "action_snapshot": {
            "decision": "offer",
            "message": "Turn on the Charlie lamp",
            "instructions": "Turn on the Charlie lamp.",
        },
        "attention_snapshot": {"level": "normal", "sound": "chime"},
        "delivery_snapshot": {"channel": "voice"},
        "management": {"provider": "scheduler", "resource_id": "trg-old"},
    }
    trigger_instances = SimpleNamespace(
        find_one=AsyncMock(return_value=original),
        update_one=AsyncMock(),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "Australia/Sydney")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: original_due)
    monkeypatch.setattr(
        "plugins.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_instances=trigger_instances,
        )),
    )

    result = await SchedulerPlugin().replace_alert(
        instance_id="trg-old",
        message="Turn on the Charlie lamp green",
        instructions="Turn on the Charlie lamp with green color.",
    )

    assert _text(result).startswith("Notification updated.")
    trigger_instances.update_one.assert_awaited_once()
    query, update = trigger_instances.update_one.await_args.args
    assert query == {"id": "trg-old", "owner_id": "geoff"}
    changed = update["$set"]
    assert changed["due_at"] == original_due
    assert changed["action_snapshot"]["message"] == "Turn on the Charlie lamp green"
    assert changed["action_snapshot"]["instructions"] == "Turn on the Charlie lamp with green color."
    assert changed["delivery_snapshot"] == {"channel": "voice", "fallback": "none"}


@pytest.mark.asyncio
async def test_replace_alert_edits_recurring_pending_instance_without_touching_rule(monkeypatch) -> None:
    original_due = datetime(2026, 6, 5, 22, 30, 0, tzinfo=timezone.utc)
    new_due = datetime(2026, 6, 5, 23, 0, 0, tzinfo=timezone.utc)
    original = {
        "id": "trg-wake",
        "owner_id": "geoff",
        "rule_id": "rule-wake",
        "status": "pending",
        "due_at": original_due,
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
        "origin_snapshot": {
            "kind": "time",
            "fire_at": original_due,
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:30",
        },
        "action_snapshot": {"decision": "tell", "message": "Wake up"},
        "attention_snapshot": {"level": "critical", "sound": "alarm"},
        "delivery_snapshot": {},
    }
    trigger_instances = SimpleNamespace(
        find_one=AsyncMock(return_value=original),
        update_one=AsyncMock(),
    )
    trigger_rules = SimpleNamespace(
        find_one=AsyncMock(return_value={
            "id": "rule-wake",
            "origin": {"kind": "time"},
            "surface": True,
            "management": {"provider": "scheduler", "resource_id": "rule-wake"},
        }),
        update_one=AsyncMock(),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "Australia/Sydney")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: new_due)
    monkeypatch.setattr(
        "plugins.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_instances=trigger_instances,
            trigger_rules=trigger_rules,
        )),
    )

    result = await SchedulerPlugin().replace_alert(
        instance_id="trg-wake",
        when="7am tomorrow",
    )

    assert _text(result).startswith("Occurrence updated.")
    trigger_instances.update_one.assert_awaited_once()
    trigger_rules.update_one.assert_not_awaited()
    changed = trigger_instances.update_one.await_args.args[1]["$set"]
    assert changed["due_at"] == new_due
    assert changed["action_snapshot"]["message"] == "Wake up"


@pytest.mark.asyncio
async def test_replace_alert_both_ids_guides_to_date_specific_instance(monkeypatch) -> None:
    trigger_instances = SimpleNamespace(update_one=AsyncMock())
    trigger_rules = SimpleNamespace(update_one=AsyncMock())
    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr(
        "plugins.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_instances=trigger_instances,
            trigger_rules=trigger_rules,
        )),
    )

    result = await SchedulerPlugin().replace_alert(
        series_id="rule-wake",
        instance_id="trg-wake",
        when="10am",
    )

    assert _text(result).startswith("Provide exactly one of series_id or instance_id")
    assert "date-specific or one-off changes" in _text(result)
    assert "permanent/all-future recurring series changes" in _text(result)
    trigger_instances.update_one.assert_not_awaited()
    trigger_rules.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_replace_alert_rejects_recurrence_on_instance_with_concise_scope_guidance(monkeypatch) -> None:
    trigger_instances = SimpleNamespace(
        find_one=AsyncMock(return_value={
            "id": "trg-wake",
            "owner_id": "geoff",
            "status": "pending",
            "due_at": datetime(2026, 6, 5, 22, 30, 0, tzinfo=timezone.utc),
        }),
        update_one=AsyncMock(),
    )
    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr(
        "plugins.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(trigger_instances=trigger_instances)),
    )

    result = await SchedulerPlugin().replace_alert(
        instance_id="trg-wake",
        when="10:30",
        recurrence="daily",
    )

    assert _text(result) == (
        "Omit recurrence for one-off occurrence edits; "
        "use series_id only for all-future changes."
    )
    trigger_instances.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_alert_recurring_instance_preserves_series(monkeypatch) -> None:
    original_due = datetime(2026, 6, 5, 22, 30, 0, tzinfo=timezone.utc)
    next_due = datetime(2026, 6, 6, 22, 30, 0, tzinfo=timezone.utc)
    instance = {
        "id": "trg-wake",
        "owner_id": "geoff",
        "rule_id": "rule-wake",
        "status": "pending",
        "due_at": original_due,
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
    }
    rule = {
        "id": "rule-wake",
        "owner_id": "geoff",
        "origin": {
            "kind": "time",
            "fire_at": original_due,
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:30",
        },
        "action": {"decision": "tell", "message": "Wake up"},
        "attention": {"level": "critical", "sound": "alarm"},
        "delivery": {},
        "surface": True,
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
    }
    cancel_instance = AsyncMock()
    create_instance = AsyncMock()

    trigger_rules = SimpleNamespace(
        find_one=AsyncMock(return_value=rule),
        delete_one=AsyncMock(),
    )
    trigger_instances = SimpleNamespace(
        find_one=AsyncMock(return_value=instance),
        update_many=AsyncMock(),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.trigger_service", SimpleNamespace(
        cancel_instance=cancel_instance,
        create_instance=create_instance,
    ))
    monkeypatch.setattr(
        "plugins.scheduler._schedule_next_occurrence",
        AsyncMock(return_value=next_due),
    )
    monkeypatch.setattr(
        "plugins.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_rules=trigger_rules,
            trigger_instances=trigger_instances,
        )),
    )

    result = await SchedulerPlugin().cancel_alert(instance_id="trg-wake")

    assert _text(result).startswith("Occurrence cancelled.")
    trigger_rules.delete_one.assert_not_awaited()
    cancel_instance.assert_awaited_once_with("trg-wake")


@pytest.mark.asyncio
async def test_cancel_alert_cancels_pending_deferred_instance(monkeypatch) -> None:
    instance = {
        "id": "trg-defer",
        "owner_id": "geoff",
        "status": "pending",
        "due_at": datetime(2026, 7, 14, 10, 0, 0, tzinfo=timezone.utc),
        "management": {"provider": "scheduler", "resource_id": "trg-defer"},
        "action_snapshot": {
            "decision": "act",
            "message": "Turn off lights",
            "instructions": "Turn off all lights in the house",
        },
    }
    cancel_instance = AsyncMock()
    publish = AsyncMock()

    trigger_instances = SimpleNamespace(
        find_one=AsyncMock(return_value=instance),
        update_many=AsyncMock(),
    )
    trigger_rules = SimpleNamespace(
        find_one=AsyncMock(return_value=None),
        delete_one=AsyncMock(),
    )

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(cancel_instance=cancel_instance),
    )
    monkeypatch.setattr("plugins.scheduler.publish_operations_changed", publish)
    monkeypatch.setattr(
        "plugins.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_rules=trigger_rules,
            trigger_instances=trigger_instances,
        )),
    )

    result = await SchedulerPlugin().cancel_alert(instance_id="trg-defer")

    assert "Notification cancelled." in _text(result)
    cancel_instance.assert_awaited_once_with("trg-defer")
    publish.assert_awaited_once_with("geoff", "schedules")


@pytest.mark.asyncio
async def test_replace_alert_retargets_series_in_place(monkeypatch) -> None:
    original_due = datetime(2026, 6, 4, 22, 30, 0, tzinfo=timezone.utc)
    rule = {
        "id": "rule-wake",
        "owner_id": "geoff",
        "origin": {
            "kind": "time",
            "fire_at": original_due,
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:30",
        },
        "action": {"decision": "tell", "message": "Wake up"},
        "attention": {"level": "critical", "requires_ack": True, "sound": "alarm"},
        "delivery": {},
        "surface": True,
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
    }
    trigger_rules = SimpleNamespace(
        find_one=AsyncMock(return_value=rule),
        update_one=AsyncMock(),
    )
    trigger_instances = SimpleNamespace(update_many=AsyncMock())
    location_ref = {
        "provider": "home_assistant",
        "room_id": "bedroom",
        "room_name": "Bedroom",
        "ha_area_id": "area-bedroom",
        "ha_device_id": None,
        "ha_entity_id": None,
    }

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "Australia/Sydney")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: original_due)
    monkeypatch.setattr("plugins.scheduler.resolve_location_ref_for_area_name", AsyncMock(return_value=location_ref))
    monkeypatch.setattr(
        "plugins.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_rules=trigger_rules,
            trigger_instances=trigger_instances,
        )),
    )

    result = await SchedulerPlugin().replace_alert(
        series_id="rule-wake",
        deliver_to="bedroom",
    )

    assert _text(result).startswith("Series updated.")
    trigger_rules.update_one.assert_awaited_once()
    rule_update = trigger_rules.update_one.await_args.args[1]["$set"]
    assert rule_update["attention"]["sound"] == "alarm"
    assert rule_update["attention"]["requires_ack"] is True
    assert rule_update["delivery"]["target"]["location_ref"]["ha_area_id"] == "area-bedroom"
    trigger_instances.update_many.assert_awaited_once()
    instance_query, instance_update = trigger_instances.update_many.await_args.args
    assert instance_query == {
        "rule_id": "rule-wake",
        "owner_id": "geoff",
        "status": "pending",
    }
    assert instance_update["$set"]["delivery_snapshot"]["target"]["location_ref"]["room_name"] == "Bedroom"


@pytest.mark.asyncio
async def test_replace_alert_invalid_delivery_target_returns_actionable_error(monkeypatch) -> None:
    original_due = datetime(2026, 6, 4, 22, 30, 0, tzinfo=timezone.utc)
    rule = {
        "id": "rule-wake",
        "owner_id": "geoff",
        "origin": {
            "kind": "time",
            "fire_at": original_due,
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:30",
        },
        "action": {"decision": "tell", "message": "Wake up"},
        "attention": {"level": "critical", "requires_ack": True, "sound": "alarm"},
        "delivery": {},
        "surface": True,
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
    }
    trigger_rules = SimpleNamespace(
        find_one=AsyncMock(return_value=rule),
        update_one=AsyncMock(),
    )
    trigger_instances = SimpleNamespace(update_many=AsyncMock())

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "Australia/Sydney")
    monkeypatch.setattr("plugins.scheduler.resolve_location_ref_for_area_name", AsyncMock(return_value=None))
    monkeypatch.setattr("plugins.scheduler.list_bound_room_names", AsyncMock(return_value=["Bedroom", "Kitchen"]))
    monkeypatch.setattr(
        "plugins.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_rules=trigger_rules,
            trigger_instances=trigger_instances,
        )),
    )

    result = await SchedulerPlugin().replace_alert(
        series_id="rule-wake",
        deliver_to="greenhouse",
    )

    assert _text(result).startswith("No bound room matched")
    assert "Bound rooms: Bedroom, Kitchen" in _text(result)
    trigger_rules.update_one.assert_not_awaited()
    trigger_instances.update_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_alarm_deliver_to_bound_room_sets_location_ref(monkeypatch) -> None:
    trigger_time = datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc)
    create_instance = AsyncMock(return_value=SimpleNamespace(id="trg-bed"))

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: trigger_time)
    monkeypatch.setattr(
        "plugins.scheduler.resolve_location_ref_for_area_name",
        AsyncMock(return_value={
            "provider": "home_assistant",
            "room_id": "bedroom",
            "room_name": "Bedroom",
            "ha_area_id": "area-bedroom",
            "ha_device_id": None,
            "ha_entity_id": None,
        }),
    )
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_instance=create_instance),
    )

    result = await SchedulerPlugin().add_alarm("7am", message="Wake up", deliver_to="bedroom")

    assert "set for" in _text(result) or "scheduled" in _text(result)
    target = create_instance.await_args.kwargs["delivery"].target
    assert target is not None
    assert target.location_ref is not None
    assert target.location_ref["ha_area_id"] == "area-bedroom"
    assert target.node_id is None


@pytest.mark.asyncio
async def test_add_alarm_unknown_room_returns_recoverable_error(monkeypatch) -> None:
    trigger_time = datetime(2026, 5, 8, 7, 0, tzinfo=timezone.utc)
    create_instance = AsyncMock()

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: trigger_time)
    monkeypatch.setattr("plugins.scheduler.resolve_location_ref_for_area_name", AsyncMock(return_value=None))
    monkeypatch.setattr("plugins.scheduler.list_bound_room_names", AsyncMock(return_value=["Bedroom"]))
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_instance=create_instance),
    )

    result = await SchedulerPlugin().add_alarm("7am", message="Wake up", deliver_to="greenhouse")

    assert "No bound room matched" in _text(result)
    assert "Bound rooms: Bedroom" in _text(result)
    assert 'deliver_to="anywhere"' in _text(result)
    create_instance.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_timer_here_without_origin_node_returns_error(monkeypatch) -> None:
    trigger_time = datetime(2026, 5, 8, 7, 5, tzinfo=timezone.utc)
    create_instance = AsyncMock()

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: trigger_time)
    monkeypatch.setattr("plugins.scheduler.get_ctx", lambda: {})
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_instance=create_instance),
    )

    result = await SchedulerPlugin().add_timer("5m", deliver_to="here")

    assert 'deliver_to="here" requires a live originating node' in _text(result)
    assert 'deliver_to="here" requires a live originating node' in _text(result)
    create_instance.assert_not_awaited()


@pytest.mark.asyncio
async def test_snooze_alert_without_identifier_uses_latest_ackable(monkeypatch) -> None:
    snooze_until = datetime(2026, 5, 8, 10, 17, tzinfo=timezone.utc)
    get_ackable_for_owner = AsyncMock(return_value={
        "id": "trg-alarm",
        "management": {"provider": "scheduler", "resource_id": "trg-alarm"},
    })
    snooze_instance = AsyncMock(return_value=SimpleNamespace(id="trg-child"))

    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr("plugins.scheduler.parse_schedule_time", lambda *_, **__: snooze_until)
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(
            get_ackable_for_owner=get_ackable_for_owner,
            snooze_instance=snooze_instance,
        ),
    )

    result = await SchedulerPlugin().snooze_alert(duration="17m")

    assert _text(result).startswith("Snoozed.")
    get_ackable_for_owner.assert_awaited_once_with("geoff")
    snooze_instance.assert_awaited_once_with("trg-alarm", snooze_until=snooze_until)


@pytest.mark.asyncio
async def test_recurring_materialization_uses_current_rule_not_stale_instance(monkeypatch) -> None:
    now = datetime(2026, 6, 4, 22, 30, tzinfo=timezone.utc)
    rule_doc = {
        "id": "rule-wake",
        "enabled": True,
        "origin": {
            "kind": "time",
            "fire_at": now,
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:45",
        },
        "action": {"decision": "tell", "message": "Wake up updated"},
        "attention": {"level": "critical", "requires_ack": True, "sound": "alarm"},
        "delivery": {
            "target": {
                "location_ref": {
                    "room_id": "bedroom",
                    "room_name": "Bedroom",
                    "ha_area_id": "area-bedroom",
                },
            },
        },
        "freshness": {"on_expiry": "expire", "stale_if_source_event_started": False},
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
    }
    stale_instance = {
        "id": "trg-old",
        "owner_id": "geoff",
        "origin_snapshot": {
            "kind": "time",
            "fire_at": now,
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:30",
        },
        "action_snapshot": {"decision": "tell", "message": "Wake up stale"},
        "attention_snapshot": {"level": "normal", "sound": "chime"},
        "delivery_snapshot": {},
    }
    materialize = AsyncMock(return_value=SimpleNamespace(id="trg-next"))
    db = SimpleNamespace(
        trigger_rules=SimpleNamespace(find_one=AsyncMock(return_value=rule_doc)),
    )
    monkeypatch.setattr("core.triggers.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr(
        "core.triggers.service.trigger_service",
        SimpleNamespace(materialize_recurring_occurrence=materialize),
    )

    await TriggerScheduler()._maybe_schedule_next("rule-wake", stale_instance, now)

    materialize.assert_awaited_once()
    kwargs = materialize.await_args.kwargs
    assert kwargs["action"].message == "Wake up updated"
    assert kwargs["attention"].sound == "alarm"
    assert kwargs["delivery"].target.location_ref["room_name"] == "Bedroom"
    assert kwargs["freshness"].on_expiry == "expire"
    assert kwargs["origin"].original_local_time == "08:45"


@pytest.mark.asyncio
async def test_maybe_schedule_next_skips_when_pending_exists(monkeypatch) -> None:
    now = datetime(2026, 6, 4, 22, 30, tzinfo=timezone.utc)
    rule_doc = {
        "id": "rule-wake",
        "enabled": True,
        "origin": {
            "kind": "time",
            "fire_at": now,
            "recurrence": "daily",
            "timezone": "Australia/Sydney",
            "original_local_time": "08:30",
        },
        "action": {"decision": "tell", "message": "Wake up"},
        "attention": {"level": "critical", "sound": "alarm"},
        "delivery": {},
        "freshness": {"on_expiry": "expire", "stale_if_source_event_started": False},
        "management": {"provider": "scheduler", "resource_id": "rule-wake"},
    }
    materialize = AsyncMock(return_value=None)
    db = SimpleNamespace(
        trigger_rules=SimpleNamespace(find_one=AsyncMock(return_value=rule_doc)),
    )
    monkeypatch.setattr("core.triggers.scheduler.mongodb", SimpleNamespace(db=db))
    monkeypatch.setattr(
        "core.triggers.service.trigger_service",
        SimpleNamespace(materialize_recurring_occurrence=materialize),
    )

    await TriggerScheduler()._maybe_schedule_next(
        "rule-wake",
        {"owner_id": "geoff", "id": "trg-old"},
        now,
    )

    materialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_add_alarm_duration_uses_alarm_preset(monkeypatch) -> None:
    fixed_now = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    create_instance = AsyncMock(return_value=SimpleNamespace(id="trg-alarm"))

    class FrozenDatetime:
        def __init__(self, frozen: datetime) -> None:
            self._frozen = frozen

        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return self._frozen

    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime(fixed_now))
    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_instance=create_instance),
    )

    result = await SchedulerPlugin().add_alarm("30 minutes from now")

    assert "set for" in _text(result) or "scheduled" in _text(result)
    attention = create_instance.await_args.kwargs["attention"]
    assert attention.requires_ack is True
    assert attention.sound == "alarm"
    assert create_instance.await_args.kwargs["origin"].kind == "time"
    assert create_instance.await_args.kwargs["origin"].fire_at == fixed_now + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_add_timer_clock_time_counts_down(monkeypatch) -> None:
    fixed_now = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    create_instance = AsyncMock(return_value=SimpleNamespace(id="trg-timer"))

    class FrozenDatetime:
        def __init__(self, frozen: datetime) -> None:
            self._frozen = frozen

        def __call__(self, *args, **kwargs):
            return dt_stdlib.datetime(*args, **kwargs)

        def now(self, tz=None):
            return self._frozen

    monkeypatch.setattr("plugins.scheduler.datetime", FrozenDatetime(fixed_now))
    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.get_timezone", lambda: "UTC")
    monkeypatch.setattr(
        "plugins.scheduler.trigger_service",
        SimpleNamespace(create_instance=create_instance),
    )

    result = await SchedulerPlugin().add_timer("5pm", deliver_to="anywhere")

    assert "set for" in _text(result) or "scheduled" in _text(result)
    origin = create_instance.await_args.kwargs["origin"]
    assert origin.kind == "interval"
    assert origin.duration_s == 7 * 3600


@pytest.mark.asyncio
async def test_remind_rejects_countdown_duration(monkeypatch) -> None:
    monkeypatch.setattr("plugins.scheduler.get_owner_id", lambda: "geoff")
    monkeypatch.setattr("plugins.scheduler.is_duration_expression", lambda _v: True)

    result = await SchedulerPlugin().remind("30m", "Check oven")

    assert "countdown duration" in _text(result)
    assert "countdown duration" in _text(result)
    assert "add_timer" in _text(result)
    assert "add_alarm" in _text(result)
