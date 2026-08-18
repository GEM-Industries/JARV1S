from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.operations.definitions import SetupSummary
from core.operations.setups import (
    SetupMutationError,
    SetupPatch,
    delete_scheduler_rule,
    patch_rule_lifecycle,
)
from core.triggers.models import TriggerRule


def _rule(*, enabled: bool = True, origin_kind: str = "time") -> dict:
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    origin: dict = {"kind": origin_kind, "fire_at": now}
    if origin_kind == "external":
        origin = {"kind": "external", "source": "slack", "event": "mention"}
    return {
        "id": "rule-1",
        "owner_id": "owner-1",
        "name": "Morning reminder",
        "enabled": enabled,
        "surface": True,
        "created_at": now,
        "updated_at": now,
        "origin": origin,
        "conditions": [],
        "action": {"decision": "tell", "message": "Good morning"},
        "attention": {},
        "delivery": {},
        "freshness": {},
        "management": {
            "provider": "automations" if origin_kind == "external" else "scheduler",
            "resource_id": "rule-1",
        },
    }


@pytest.mark.asyncio
async def test_patch_rule_lifecycle_validates_owner_and_publishes_scope(monkeypatch):
    collection = SimpleNamespace(
        find_one=AsyncMock(return_value=_rule()),
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)),
    )
    instances = SimpleNamespace(update_many=AsyncMock(return_value=SimpleNamespace(modified_count=0)))
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
    list_projection = AsyncMock(return_value=[summary])
    publish = AsyncMock()
    monkeypatch.setattr("core.operations.setups.list_setups", list_projection)
    monkeypatch.setattr("core.operations.setups.publish_operations_changed", publish)

    result = await patch_rule_lifecycle(
        "owner-1",
        "rule:rule-1",
        SetupPatch(enabled=False),
    )

    assert result == summary
    collection.update_one.assert_awaited_once()
    update = collection.update_one.await_args.args[1]["$set"]
    assert update["enabled"] is False
    publish.assert_awaited_once_with("owner-1", "schedules")


@pytest.mark.asyncio
async def test_patch_rule_lifecycle_rejects_protocol_mutation():
    with pytest.raises(SetupMutationError, match="read-only"):
        await patch_rule_lifecycle(
            "owner-1",
            "protocol:morning",
            SetupPatch(enabled=False),
        )


@pytest.mark.asyncio
async def test_delete_scheduler_rule_removes_rule_and_cancels_open_instances(monkeypatch):
    rules = SimpleNamespace(
        find_one=AsyncMock(return_value=_rule(enabled=False)),
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)),
        delete_one=AsyncMock(return_value=SimpleNamespace(deleted_count=1)),
    )
    instances = SimpleNamespace(update_many=AsyncMock())
    monkeypatch.setattr(
        "core.operations.setups.mongodb",
        SimpleNamespace(
            db=SimpleNamespace(
                trigger_rules=rules,
                trigger_instances=instances,
            )
        ),
    )
    monkeypatch.setattr(
        "core.triggers.lifecycle.mongodb",
        SimpleNamespace(
            db=SimpleNamespace(
                trigger_rules=rules,
                trigger_instances=instances,
            )
        ),
    )
    publish = AsyncMock()
    monkeypatch.setattr("core.operations.setups.publish_operations_changed", publish)

    deleted = await delete_scheduler_rule("owner-1", "rule:rule-1")

    assert isinstance(deleted, TriggerRule)
    assert deleted.id == "rule-1"
    rules.delete_one.assert_awaited_once()
    instances.update_many.assert_awaited_once()
    publish.assert_awaited_once_with("owner-1", "schedules")


@pytest.mark.asyncio
async def test_delete_scheduler_rule_rejects_external_rules(monkeypatch):
    rules = SimpleNamespace(
        find_one=AsyncMock(return_value=_rule(origin_kind="external")),
    )
    monkeypatch.setattr(
        "core.operations.setups.mongodb",
        SimpleNamespace(db=SimpleNamespace(trigger_rules=rules)),
    )

    with pytest.raises(SetupMutationError, match="scheduler-managed"):
        await delete_scheduler_rule("owner-1", "rule-1")


@pytest.mark.asyncio
async def test_delete_scheduler_rule_rejects_protocol():
    with pytest.raises(SetupMutationError, match="read-only"):
        await delete_scheduler_rule("owner-1", "protocol:morning")
