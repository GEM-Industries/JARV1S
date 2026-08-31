"""Lifecycle safety tests for pause/disable and parent-rule guards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.operations.definitions import SetupSummary
from core.operations.setups import SetupPatch, patch_rule_lifecycle
from core.triggers.lifecycle import materialize_after_pause, rule_allows_dispatch
from core.triggers.models import TriggerRule
from core.triggers.scheduler import TriggerScheduler


def _rule(*, enabled: bool = True, paused_until: datetime | None = None) -> TriggerRule:
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    return TriggerRule.model_validate(
        {
            "id": "rule-1",
            "owner_id": "owner-1",
            "name": "Morning reminder",
            "enabled": enabled,
            "surface": True,
            "created_at": now,
            "updated_at": now,
            "origin": {"kind": "time", "fire_at": now},
            "conditions": [],
            "action": {"decision": "tell", "message": "Good morning"},
            "attention": {},
            "delivery": {},
            "freshness": {},
            "paused_until": paused_until,
            "management": {"provider": "scheduler", "resource_id": "rule-1"},
        }
    )


def test_rule_allows_dispatch_respects_enabled_and_pause() -> None:
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    assert rule_allows_dispatch(_rule(enabled=True), now=now)
    assert not rule_allows_dispatch(_rule(enabled=False), now=now)
    assert not rule_allows_dispatch(
        _rule(enabled=True, paused_until=now + timedelta(hours=1)),
        now=now,
    )


@pytest.mark.asyncio
async def test_finite_pause_materializes_first_occurrence_after_pause(monkeypatch) -> None:
    paused_until = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)
    next_due = paused_until + timedelta(days=1)
    rule = _rule(paused_until=paused_until).model_copy(
        update={
            "origin": _rule().origin.model_copy(update={"recurrence": "daily"}),
        }
    )
    materialize = AsyncMock()
    monkeypatch.setattr("core.scheduling.next_occurrence", lambda *_args, **_kwargs: next_due)
    monkeypatch.setattr("core.scheduling.recurrence_rule_from_origin", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        "core.triggers.service.trigger_service.materialize_recurring_occurrence",
        materialize,
    )

    await materialize_after_pause(rule, paused_until)

    assert materialize.await_args.kwargs["due_at"] == next_due
    assert materialize.await_args.kwargs["rule_id"] == rule.id


@pytest.mark.asyncio
async def test_indefinite_pause_does_not_materialize(monkeypatch) -> None:
    from core.triggers.lifecycle import INDEFINITE_PAUSE

    rule = _rule().model_copy(
        update={
            "origin": _rule().origin.model_copy(update={"recurrence": "daily"}),
        }
    )
    materialize = AsyncMock()
    monkeypatch.setattr(
        "core.triggers.service.trigger_service.materialize_recurring_occurrence",
        materialize,
    )

    await materialize_after_pause(rule, INDEFINITE_PAUSE)

    materialize.assert_not_awaited()


@pytest.mark.asyncio
async def test_patch_rule_lifecycle_cancels_open_instances_on_disable(monkeypatch) -> None:
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value=_rule().model_dump(mode="python")),
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)),
    )
    instances = SimpleNamespace(update_many=AsyncMock(return_value=SimpleNamespace(modified_count=2)))
    monkeypatch.setattr(
        "core.operations.setups.mongodb",
        SimpleNamespace(
            db=SimpleNamespace(
                trigger_rules=collection,
                trigger_instances=instances,
            )
        ),
    )
    monkeypatch.setattr(
        "core.triggers.lifecycle.mongodb",
        SimpleNamespace(
            db=SimpleNamespace(
                trigger_rules=collection,
                trigger_instances=instances,
            )
        ),
    )
    summary = SetupSummary(
        id="rule:rule-1",
        source="trigger_rule",
        kind="reminder",
        name="Morning reminder",
        enabled=False,
        status="disabled",
    )
    monkeypatch.setattr("core.operations.setups.list_setups", AsyncMock(return_value=[summary]))
    monkeypatch.setattr("core.operations.setups.publish_operations_changed", AsyncMock())

    await patch_rule_lifecycle("owner-1", "rule:rule-1", SetupPatch(enabled=False))

    instances.update_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_rule_lifecycle_resume_materializes_next_occurrence(monkeypatch) -> None:
    rule = _rule(enabled=False).model_copy(
        update={"origin": _rule().origin.model_copy(update={"recurrence": "daily"})}
    )
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value=rule.model_dump(mode="python")),
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)),
    )
    monkeypatch.setattr(
        "core.operations.setups.mongodb",
        SimpleNamespace(db=SimpleNamespace(trigger_rules=collection, trigger_instances=SimpleNamespace())),
    )
    summary = SetupSummary(
        id="rule:rule-1",
        source="trigger_rule",
        kind="reminder",
        name="Morning reminder",
        enabled=True,
        status="active",
    )
    monkeypatch.setattr("core.operations.setups.list_setups", AsyncMock(return_value=[summary]))
    monkeypatch.setattr("core.operations.setups.publish_operations_changed", AsyncMock())
    materialize = AsyncMock()
    monkeypatch.setattr("core.operations.setups.materialize_after_pause", materialize)

    await patch_rule_lifecycle(
        "owner-1",
        "rule:rule-1",
        SetupPatch(enabled=True, paused_until=None),
    )

    materialize.assert_awaited_once()
    assert materialize.await_args.args[0].enabled is True
    assert materialize.await_args.args[0].paused_until is None


@pytest.mark.asyncio
async def test_scheduler_skips_due_instance_when_parent_rule_disabled(monkeypatch) -> None:
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    pending = {
        "id": "trg-1",
        "owner_id": "owner-1",
        "rule_id": "rule-1",
        "status": "pending",
        "due_at": now - timedelta(minutes=1),
    }
    rules = SimpleNamespace(
        find_one=AsyncMock(return_value=_rule(enabled=False).model_dump(mode="python")),
    )
    instances = SimpleNamespace(
        distinct=AsyncMock(return_value=[]),
        find_one=AsyncMock(side_effect=[pending, None]),
        find_one_and_update=AsyncMock(),
        update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)),
    )
    monkeypatch.setattr(
        "core.triggers.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(trigger_rules=rules, trigger_instances=instances)),
    )
    publish = AsyncMock()
    monkeypatch.setattr("core.triggers.scheduler.event_bus.publish", publish)

    await TriggerScheduler(poll_interval=1)._process_due()

    instances.update_one.assert_awaited()
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_cancels_due_instance_when_parent_rule_is_missing(monkeypatch) -> None:
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    pending = {
        "id": "trg-orphan",
        "owner_id": "owner-1",
        "rule_id": "missing-rule",
        "status": "pending",
        "due_at": now - timedelta(minutes=1),
    }
    rules = SimpleNamespace(find_one=AsyncMock(return_value=None))
    instances = SimpleNamespace(
        distinct=AsyncMock(return_value=[]),
        find_one=AsyncMock(side_effect=[pending, None]),
        find_one_and_update=AsyncMock(),
        update_one=AsyncMock(return_value=SimpleNamespace(modified_count=1)),
    )
    monkeypatch.setattr(
        "core.triggers.scheduler.mongodb",
        SimpleNamespace(db=SimpleNamespace(trigger_rules=rules, trigger_instances=instances)),
    )
    publish = AsyncMock()
    monkeypatch.setattr("core.triggers.scheduler.event_bus.publish", publish)

    await TriggerScheduler(poll_interval=1)._process_due()

    update = instances.update_one.await_args.args[1]["$set"]
    assert update["status"] == "cancelled"
    assert update["failure_reason"] == "parent_rule_missing"
    publish.assert_not_awaited()
