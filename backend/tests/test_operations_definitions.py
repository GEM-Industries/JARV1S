from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.operations.definitions import explain_setup, list_automation_definitions, list_setups


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
        self.queries = []

    def find(self, query, projection=None):
        self.queries.append(query)
        docs = list(self.docs)
        if owner_id := query.get("owner_id"):
            docs = [doc for doc in docs if doc.get("owner_id") == owner_id]
        if query.get("surface") is True:
            docs = [doc for doc in docs if doc.get("surface") is True]
        origin_kind = query.get("origin.kind")
        if isinstance(origin_kind, dict) and "$in" in origin_kind:
            allowed = set(origin_kind["$in"])
            docs = [doc for doc in docs if doc.get("origin", {}).get("kind") in allowed]
        elif origin_kind:
            docs = [doc for doc in docs if doc.get("origin", {}).get("kind") == origin_kind]
        if "rule_id" in query:
            allowed = set(query["rule_id"]["$in"])
            docs = [doc for doc in docs if doc.get("rule_id") in allowed]
        if "$or" in query:
            allowed = set()
            for clause in query["$or"]:
                if "rule_id" in clause:
                    allowed.update(clause["rule_id"]["$in"])
                if "source_event.rule_id" in clause:
                    allowed.update(clause["source_event.rule_id"]["$in"])
            docs = [
                doc for doc in docs
                if doc.get("rule_id") in allowed or doc.get("source_event", {}).get("rule_id") in allowed
            ]
        if "status" in query:
            allowed = set(query["status"]["$in"])
            docs = [doc for doc in docs if doc.get("status") in allowed]
        if "name" in query and "$in" in query["name"]:
            allowed = set(query["name"]["$in"])
            docs = [doc for doc in docs if doc.get("name") in allowed]
        return FakeCursor(docs)

    async def find_one(self, query, projection=None):
        cursor = self.find(query, projection)
        docs = await cursor.to_list(length=1)
        return docs[0] if docs else None


@pytest.mark.asyncio
async def test_list_setups_projects_surfaced_rules_with_protocol(monkeypatch):
    now = datetime(2026, 6, 3, 8, tzinfo=timezone.utc)
    rules = [
        {
            "id": "rule-1",
            "owner_id": "owner-1",
            "name": "Standup prep",
            "description": None,
            "enabled": True,
            "surface": True,
            "created_at": now,
            "updated_at": now,
            "origin": {"kind": "time", "fire_at": now, "recurrence": "daily"},
            "conditions": [],
            "action": {"decision": "tell", "message": "Prep", "protocol_name": "Morning"},
            "attention": {"level": "normal", "sound": "chime"},
            "delivery": {},
            "freshness": {},
            "management": {"provider": "scheduler", "resource_id": "rule-1"},
        },
        {
            "id": "rule-hidden",
            "owner_id": "owner-1",
            "name": "Habit check-in: Pushups",
            "enabled": True,
            "surface": False,
            "created_at": now,
            "updated_at": now,
            "origin": {"kind": "time", "fire_at": now},
            "conditions": [],
            "action": {"decision": "tell", "message": "Check in"},
            "attention": {"level": "normal", "sound": "chime"},
            "delivery": {},
            "freshness": {},
            "management": {"provider": "habits", "resource_id": "habit-1"},
        },
    ]
    instances = [
        {"id": "trg-next", "owner_id": "owner-1", "rule_id": "rule-1", "status": "pending", "due_at": now},
        {
            "id": "trg-last",
            "owner_id": "owner-1",
            "rule_id": "rule-1",
            "status": "delivered",
            "updated_at": now,
        },
    ]
    protocols = [
        {
            "id": "protocol-morning",
            "owner_id": "owner-1",
            "name": "Morning",
            "description": "Morning prep",
            "steps": ["Check calendar"],
            "prefetch_safe": True,
        }
    ]
    fake_db = SimpleNamespace(
        trigger_rules=FakeCollection(rules),
        trigger_instances=FakeCollection(instances),
        protocols=FakeCollection(protocols),
    )
    monkeypatch.setattr("core.operations.definitions.mongodb", SimpleNamespace(db=fake_db))

    rows = await list_setups("owner-1")

    assert [row.name for row in rows] == ["Standup prep"]
    assert rows[0].id == "rule:rule-1"
    assert rows[0].series_id == "rule-1"
    assert rows[0].rule_id is None
    assert rows[0].kind == "schedule"
    assert rows[0].next_due_at == now
    assert rows[0].last_outcome == "delivered"
    assert rows[0].linked_protocol is not None
    assert rows[0].linked_protocol.prefetch_safe is True

@pytest.mark.asyncio
async def test_explain_distinguishes_no_instance_from_failed_instance(monkeypatch):
    now = datetime(2026, 6, 3, 8, tzinfo=timezone.utc)
    base_rule = {
        "owner_id": "owner-1",
        "description": None,
        "enabled": True,
        "surface": True,
        "created_at": now,
        "updated_at": now,
        "origin": {"kind": "time", "fire_at": now},
        "conditions": [],
        "action": {"decision": "tell", "message": "Reminder"},
        "attention": {"level": "normal", "sound": "chime"},
        "delivery": {},
        "freshness": {},
    }
    rules = [
        {
            "id": "rule-empty",
            "name": "No fire",
            **base_rule,
            "management": {"provider": "scheduler", "resource_id": "rule-empty"},
        },
        {
            "id": "rule-failed",
            "name": "Bad fire",
            **base_rule,
            "management": {"provider": "scheduler", "resource_id": "rule-failed"},
        },
    ]
    instances = [
        {
            "id": "trg-failed",
            "owner_id": "owner-1",
            "rule_id": "rule-failed",
            "status": "failed",
            "updated_at": now,
            "due_at": now,
            "created_at": now,
            "origin_snapshot": {"kind": "time"},
            "action_snapshot": {"decision": "tell", "message": "Reminder"},
            "source_event": {},
            "failure_reason": "delivery_ttl_expired",
        },
    ]
    fake_db = SimpleNamespace(
        trigger_rules=FakeCollection(rules),
        trigger_instances=FakeCollection(instances),
        protocols=FakeCollection([]),
        conversations=FakeCollection([]),
        turn_runs=FakeCollection([]),
        protocol_runs=FakeCollection([]),
    )
    monkeypatch.setattr("core.operations.definitions.mongodb", SimpleNamespace(db=fake_db))
    monkeypatch.setattr("core.operations.service.mongodb", SimpleNamespace(db=fake_db))

    no_fire = await explain_setup("owner-1", "No fire")
    failed = await explain_setup("owner-1", "Bad fire")

    assert no_fire is not None and not isinstance(no_fire, list)
    assert "No trigger instance" in no_fire.diagnosis
    assert failed is not None and not isinstance(failed, list)
    assert failed.latest_instance_id == "trg-failed"
    assert failed.failure_label == "Delivery window expired"


@pytest.mark.asyncio
async def test_protocol_setups_are_explicitly_filtered_and_explainable(monkeypatch):
    now = datetime(2026, 6, 3, 8, tzinfo=timezone.utc)
    protocols = [
        {
            "id": "protocol-morning",
            "owner_id": "owner-1",
            "name": "Morning",
            "description": "Morning prep",
            "steps": ["Check calendar"],
            "prefetch_safe": True,
            "last_run_at": now,
        }
    ]
    fake_db = SimpleNamespace(
        trigger_rules=FakeCollection([]),
        trigger_instances=FakeCollection([]),
        protocols=FakeCollection(protocols),
    )
    monkeypatch.setattr("core.operations.definitions.mongodb", SimpleNamespace(db=fake_db))

    assert await list_setups("owner-1", kind="protocol", status="disabled") == []
    rows = await list_setups("owner-1", kind="protocol")
    explained = await explain_setup("owner-1", rows[0].id)

    assert rows[0].id == "protocol:protocol-morning"
    assert rows[0].linked_protocol is not None
    assert rows[0].linked_protocol.prefetch_safe is True
    assert explained is not None and not isinstance(explained, list)
    assert explained.diagnosis == "This is a saved routine; it has no trigger instance history."


@pytest.mark.asyncio
async def test_list_automation_definitions_validates_full_trigger_rule_docs(monkeypatch):
    now = datetime(2026, 6, 3, 8, tzinfo=timezone.utc)
    rules = [
        {
            "id": "auto-1",
            "owner_id": "owner-1",
            "name": "Meeting Reminder",
            "description": None,
            "enabled": True,
            "surface": True,
            "created_at": now,
            "updated_at": now,
            "origin": {
                "kind": "external",
                "source": "calendar",
                "event": "starting",
                "offset_minutes": -1,
            },
            "conditions": [],
            "action": {"decision": "tell", "message": "Meeting soon"},
            "attention": {"level": "urgent", "sound": "chime"},
            "delivery": {},
            "freshness": {"stale_if_source_event_started": True},
            "management": {"provider": "automations", "resource_id": "auto-1"},
        }
    ]
    instances = [
        {
            "id": "trg-1",
            "owner_id": "owner-1",
            "source_event": {"rule_id": "auto-1"},
            "status": "delivered",
            "updated_at": now,
        },
        {
            "id": "trg-2",
            "owner_id": "owner-1",
            "source_event": {"rule_id": "auto-1"},
            "status": "completed",
            "updated_at": datetime(2026, 6, 2, 8, tzinfo=timezone.utc),
        },
    ]
    fake_db = SimpleNamespace(
        trigger_rules=FakeCollection(rules),
        trigger_instances=FakeCollection(instances),
        protocols=FakeCollection([]),
    )
    monkeypatch.setattr("core.operations.definitions.mongodb", SimpleNamespace(db=fake_db))

    rows = await list_automation_definitions("owner-1")

    assert len(rows) == 1
    assert rows[0].id == "auto-1"
    assert rows[0].importance == "urgent"
    assert rows[0].trigger == {"source": "calendar", "event": "starting", "offset": -1}
    assert rows[0].last_run_at == now
    assert rows[0].run_count == 2
