from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import api.routes.schedules as schedules_route
from core.config import settings


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_args):
        return self

    async def to_list(self, *args, **kwargs):
        return self.docs


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.query = None
        self.projection = None

    def find(self, query, projection=None):
        self.query = query
        self.projection = projection
        docs = self.docs
        if query.get("enabled") is True:
            docs = [doc for doc in docs if doc.get("enabled") is True]
        if query.get("surface") is True:
            docs = [doc for doc in docs if doc.get("surface") is True]
        if "origin.kind" in query:
            allowed = set(query["origin.kind"]["$in"])
            docs = [doc for doc in docs if doc.get("origin", {}).get("kind") in allowed]
        return FakeCursor(docs)


@pytest.mark.asyncio
async def test_list_schedules_defaults_to_active_with_display_fields(monkeypatch):
    fire_at = datetime(2026, 6, 2, 20, 15, tzinfo=timezone.utc)
    next_due = datetime(2026, 6, 3, 20, 15, tzinfo=timezone.utc)
    trigger_rules = FakeCollection([
        {
            "id": "rule-active",
            "name": "Wake up",
            "enabled": True,
            "surface": True,
            "origin": {
                "kind": "time",
                "fire_at": fire_at,
                "recurrence": "daily",
                "timezone": "Australia/Sydney",
                "original_local_time": "06:15",
            },
            "action": {"kind": "notify"},
            "updated_at": fire_at,
        },
        {
            "id": "rule-disabled",
            "name": "Old wake up",
            "enabled": False,
            "surface": True,
            "origin": {"kind": "time", "recurrence": "daily"},
            "action": {"kind": "notify"},
            "updated_at": fire_at,
        },
    ])
    trigger_instances = FakeCollection([
        {"rule_id": "rule-active", "due_at": next_due},
    ])
    monkeypatch.setattr(
        schedules_route,
        "mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_rules=trigger_rules,
            trigger_instances=trigger_instances,
        )),
    )

    result = await schedules_route.list_schedules(owner_id=settings.DEFAULT_USER_ID)

    assert trigger_rules.query == {
        "owner_id": settings.DEFAULT_USER_ID,
        "origin.kind": {"$in": ["time", "interval"]},
        "surface": True,
        "enabled": True,
    }
    assert len(result) == 1
    assert result[0].id == "rule-active"
    assert result[0].recurrence_label == "Daily at 6:15 AM"
    assert result[0].next_due_at == next_due


@pytest.mark.asyncio
async def test_list_schedules_can_include_disabled(monkeypatch):
    trigger_rules = FakeCollection([
        {
            "id": "rule-disabled",
            "name": "Old wake up",
            "enabled": False,
            "surface": True,
            "origin": {"kind": "time", "recurrence": "daily"},
            "action": {"kind": "notify"},
        },
    ])
    monkeypatch.setattr(
        schedules_route,
        "mongodb",
        SimpleNamespace(db=SimpleNamespace(
            trigger_rules=trigger_rules,
            trigger_instances=FakeCollection([]),
        )),
    )

    result = await schedules_route.list_schedules(
        include_disabled=True,
        owner_id=settings.DEFAULT_USER_ID,
    )

    assert trigger_rules.query == {
        "owner_id": settings.DEFAULT_USER_ID,
        "origin.kind": {"$in": ["time", "interval"]},
        "surface": True,
    }
    assert len(result) == 1
    assert result[0].enabled is False
