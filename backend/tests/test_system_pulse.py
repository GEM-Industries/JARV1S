"""Unit tests for Phase 9b SystemPulse.

Covers _tick behavior with mocked mongodb / event_bus. No real DB required.

Run from backend/: `pytest tests/test_system_pulse.py`
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.system_pulse import SystemPulse


@pytest.fixture
def pulse():
    return SystemPulse(
        interval_min=15,
        owner_id="test_owner",
    )


def _cursor(items: list[dict]):
    """Build an AsyncMock that mimics Motor's find() cursor .to_list()."""
    cursor = AsyncMock()
    cursor.to_list = AsyncMock(return_value=items)
    return cursor


def _mock_db(
    *,
    overdue=None,
    stuck_executing=None,
    failures=None,
    awaiting_delivery=None,
    last_escalated=None,
):
    """Patch mongodb.db with collection mocks that route by query shape."""

    def trigger_instances_find(query):
        status = query.get("status")
        if isinstance(status, dict) and "$in" in status:
            return _cursor(stuck_executing or [])
        if status == "pending":
            return _cursor(overdue or [])
        if status == "awaiting_delivery":
            return _cursor(awaiting_delivery or [])
        return _cursor([])

    def automation_fired_find(query):
        return _cursor(failures or [])

    trigger_instances = AsyncMock()
    trigger_instances.find = MagicMock(side_effect=trigger_instances_find)
    automation_fired = AsyncMock()
    automation_fired.find = MagicMock(side_effect=automation_fired_find)
    pulse_runs = AsyncMock()
    pulse_runs.find_one = AsyncMock(return_value=last_escalated)
    pulse_runs.insert_one = AsyncMock()

    db = AsyncMock()
    db.trigger_instances = trigger_instances
    db.automation_fired = automation_fired
    db.pulse_runs = pulse_runs
    return db


class TestTick:
    @pytest.mark.asyncio
    async def test_runs_without_attention_gate(self, pulse):
        db = _mock_db()
        with patch("services.system_pulse.mongodb") as mock_mongo, \
             patch("services.system_pulse.event_bus") as mock_bus:
            mock_mongo.db = db
            mock_bus.publish = AsyncMock()
            await pulse._tick()

            assert db.trigger_instances.find.call_count >= 1
            db.pulse_runs.insert_one.assert_awaited_once()
            logged = db.pulse_runs.insert_one.await_args.args[0]
            assert logged["reason"] == "empty"
            mock_bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_findings_logs_no_publish(self, pulse):
        db = _mock_db()
        with patch("services.system_pulse.mongodb") as mock_mongo, \
             patch("services.system_pulse.event_bus") as mock_bus:
            mock_mongo.db = db
            mock_bus.publish = AsyncMock()
            await pulse._tick()

            db.pulse_runs.insert_one.assert_awaited_once()
            logged = db.pulse_runs.insert_one.await_args.args[0]
            assert logged["escalated"] is False
            assert logged["reason"] == "empty"
            mock_bus.publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_overdue_trigger_escalates(self, pulse):
        now = datetime.now(timezone.utc)
        overdue = [{
            "id": "trg-a1",
            "action_snapshot": {"kind": "notify", "message": "meeting"},
            "due_at": now - timedelta(minutes=30),
            "owner_id": "test_owner",
        }]
        db = _mock_db(overdue=overdue)
        notif_insert = AsyncMock(return_value=AsyncMock(inserted_id="x"))
        db.trigger_instances.insert_one = notif_insert

        with patch("services.system_pulse.mongodb") as mock_mongo, \
             patch("core.triggers.service.mongodb") as mock_notif_mongo, \
             patch("services.system_pulse.event_bus") as mock_bus:
            mock_mongo.db = db
            mock_notif_mongo.db = db
            mock_bus.publish = AsyncMock()
            await pulse._tick()

            mock_bus.publish.assert_awaited_once()
            event = mock_bus.publish.await_args.args[0]
            assert event.source == "system_pulse"
            assert event.type.value == "trigger.due"

            db.pulse_runs.insert_one.assert_awaited_once()
            logged = db.pulse_runs.insert_one.await_args.args[0]
            assert logged["escalated"] is True
            assert logged["reason"] == "escalated"
            assert "overdue_triggers:trg-a1" in logged["findings_keys"]
            assert "overdue_triggers:trg-a1" in logged["new_keys"]
            inserted = db.trigger_instances.insert_one.await_args.args[0]
            assert inserted["attention_snapshot"]["level"] == "normal"
            assert inserted["attention_snapshot"]["sound"] == "none"

    @pytest.mark.asyncio
    async def test_same_finding_suppressed_second_tick(self, pulse):
        now = datetime.now(timezone.utc)
        overdue = [{
            "id": "trg-a1",
            "action_snapshot": {"kind": "notify", "message": "meeting"},
            "due_at": now - timedelta(minutes=30),
            "owner_id": "test_owner",
        }]
        last = {
            "tick_at": now - timedelta(minutes=15),
            "escalated": True,
            "findings_keys": ["overdue_triggers:trg-a1"],
        }
        db = _mock_db(overdue=overdue, last_escalated=last)

        with patch("services.system_pulse.mongodb") as mock_mongo, \
             patch("services.system_pulse.event_bus") as mock_bus:
            mock_mongo.db = db
            mock_bus.publish = AsyncMock()
            await pulse._tick()

            mock_bus.publish.assert_not_awaited()
            db.pulse_runs.insert_one.assert_awaited_once()
            logged = db.pulse_runs.insert_one.await_args.args[0]
            assert logged["escalated"] is False
            assert logged["reason"] == "suppressed"
            assert "overdue_triggers:trg-a1" in logged["findings_keys"]

    @pytest.mark.asyncio
    async def test_new_finding_alongside_suppressed_re_escalates(self, pulse):
        now = datetime.now(timezone.utc)
        overdue = [
            {
                "id": "trg-a1",
                "action_snapshot": {"kind": "notify", "message": "meeting"},
                "due_at": now - timedelta(minutes=30),
                "owner_id": "test_owner",
            },
            {
                "id": "trg-a2",
                "action_snapshot": {"kind": "notify", "message": "standup"},
                "due_at": now - timedelta(minutes=10),
                "owner_id": "test_owner",
            },
        ]
        last = {
            "tick_at": now - timedelta(minutes=15),
            "escalated": True,
            "findings_keys": ["overdue_triggers:trg-a1"],
        }
        db = _mock_db(overdue=overdue, last_escalated=last)
        notif_insert = AsyncMock(return_value=AsyncMock(inserted_id="x"))
        db.trigger_instances.insert_one = notif_insert

        with patch("services.system_pulse.mongodb") as mock_mongo, \
             patch("core.triggers.service.mongodb") as mock_notif_mongo, \
             patch("services.system_pulse.event_bus") as mock_bus:
            mock_mongo.db = db
            mock_notif_mongo.db = db
            mock_bus.publish = AsyncMock()
            await pulse._tick()

            mock_bus.publish.assert_awaited_once()
            logged = db.pulse_runs.insert_one.await_args.args[0]
            assert logged["escalated"] is True
            assert logged["new_keys"] == ["overdue_triggers:trg-a2"]
