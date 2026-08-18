from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.habits import HabitsPlugin
from plugins.habits import store


class _Cursor:
    def __init__(self, docs: list[dict]) -> None:
        self._docs = docs

    def sort(self, field: str, direction: int):
        reverse = direction < 0
        self._docs.sort(key=lambda doc: _get_path(doc, field) or datetime.min, reverse=reverse)
        return self

    async def to_list(self, _limit):
        return list(self._docs)


class _Collection:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    async def insert_one(self, doc: dict):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id=doc.get("id"))

    async def find_one(self, filter_arg: dict):
        for doc in self.docs:
            if _matches(doc, filter_arg):
                return dict(doc)
        return None

    async def update_one(self, filter_arg: dict, update: dict):
        for doc in self.docs:
            if _matches(doc, filter_arg):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def update_many(self, filter_arg: dict, update: dict):
        matched = 0
        for doc in self.docs:
            if _matches(doc, filter_arg):
                doc.update(update.get("$set", {}))
                matched += 1
        return SimpleNamespace(matched_count=matched)

    def find(self, filter_arg: dict):
        return _Cursor([dict(doc) for doc in self.docs if _matches(doc, filter_arg)])

    async def create_index(self, *_args, **_kwargs):
        return None


class _Db:
    def __init__(self) -> None:
        self.collections = {
            store.HABITS_COLLECTION: _Collection(),
            store.HABIT_LOGS_COLLECTION: _Collection(),
            store.HABIT_CHECKIN_PLANS_COLLECTION: _Collection(),
            "trigger_rules": _Collection(),
            "trigger_instances": _Collection(),
        }

    def __getitem__(self, name: str):
        return self.collections[name]

    def __getattr__(self, name: str):
        if name in self.collections:
            return self.collections[name]
        raise AttributeError(name)


def _matches(doc: dict, filter_arg: dict) -> bool:
    for key, expected in filter_arg.items():
        actual = _get_path(doc, key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$gte" in expected and actual < expected["$gte"]:
                return False
            continue
        if actual != expected:
            return False
    return True


def _get_path(doc: dict, path: str):
    current = doc
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


@pytest.fixture
def habit_db(monkeypatch):
    db = _Db()
    monkeypatch.setattr("plugins.habits.store.mongodb", SimpleNamespace(db=db))
    return db


@pytest.mark.asyncio
async def test_create_log_and_status_use_owner_scoped_habit_data(
    habit_db, invoke_tool, tool_context
) -> None:
    plugin = HabitsPlugin()

    created = await invoke_tool(
        plugin,
        "create_habit",
        name="Reading",
        behavior="read after dinner",
        cue="after dinner",
        minimum_version="one page",
        desired_frequency="daily",
    )

    assert created.content == "Created habit 'Reading'."
    assert created.ui[0].data["display"] == "receipt"
    habit_doc = habit_db[store.HABITS_COLLECTION].docs[0]
    assert habit_doc["owner_id"] == "owner-1"
    assert habit_doc["name"] == "Reading"

    logged = await invoke_tool(plugin, "log_habit", habit_doc["id"], status="done")

    assert logged.content == "Logged Reading as done."
    assert habit_db[store.HABIT_LOGS_COLLECTION].docs[0]["status"] == "done"

    logged_by_name = await invoke_tool(
        plugin, "log_habit_by_name", " reading ", status="missed", note="got home late"
    )

    assert logged_by_name.content == "Logged Reading as missed."
    assert habit_db[store.HABIT_LOGS_COLLECTION].docs[1]["note"] == "got home late"

    status = await invoke_tool(plugin, "get_habit_status", habit_doc["id"], days=7)

    assert f"Reading (habit_id={habit_doc['id']}): 1 done, 1 missed, 0 skipped over 7 days" in status.content
    assert status.ui[0].component == "ContentWidget"

    status_by_name = await invoke_tool(plugin, "get_habit_status", "Reading", days=7)

    assert f"Reading (habit_id={habit_doc['id']}):" in status_by_name.content


@pytest.mark.asyncio
async def test_measured_habit_log_preserves_target_delta_details(
    habit_db, invoke_tool, tool_context
) -> None:
    plugin = HabitsPlugin()
    await invoke_tool(
        plugin,
        "create_habit",
        name="Consistent Sleep",
        behavior="off screens by 11 PM, asleep by midnight",
        cue="11 PM",
    )

    logged = await invoke_tool(
        plugin,
        "log_measured_habit_by_name",
        name="consistent sleep",
        metric="bedtime",
        observed_value="2:00 AM",
        target="12:00 AM",
        delta="+2h",
        status="missed",
        note="Worked late",
    )

    assert logged.content == "Logged Consistent Sleep bedtime as 2:00 AM."
    log_doc = habit_db[store.HABIT_LOGS_COLLECTION].docs[0]
    assert log_doc["status"] == "missed"
    assert log_doc["details"]["metric"] == "bedtime"
    assert log_doc["details"]["observed_value"] == "2:00 AM"
    assert log_doc["details"]["target"] == "12:00 AM"
    assert log_doc["details"]["delta"] == "+2h"

    status = await invoke_tool(
        plugin,
        "get_habit_status",
        habit_id=habit_db[store.HABITS_COLLECTION].docs[0]["id"],
    )

    assert "latest bedtime: 2:00 AM" in status.content
    assert "delta +2h" in status.content


@pytest.mark.asyncio
async def test_log_habit_refuses_unknown_habit(habit_db, invoke_tool, tool_context) -> None:
    result = await invoke_tool(
        HabitsPlugin(), "log_habit", "missing", status="missed", note="too tired"
    )

    assert result.message.startswith("Habit not found")
    assert habit_db[store.HABIT_LOGS_COLLECTION].docs == []


@pytest.mark.asyncio
async def test_status_caps_window_and_suggests_adjustment(
    habit_db, invoke_tool, tool_context
) -> None:
    plugin = HabitsPlugin()
    created = await invoke_tool(
        plugin,
        "create_habit",
        name="Stretching",
        behavior="stretch before bed",
        cue="before bed",
    )
    habit_id = habit_db[store.HABITS_COLLECTION].docs[0]["id"]
    now = datetime.now(timezone.utc)
    for index in range(3):
        await store.log_habit(
            owner_id="owner-1",
            habit_id=habit_id,
            status="missed",
            note=f"miss {index}",
            logged_at=now - timedelta(days=index),
        )

    status = await invoke_tool(plugin, "get_habit_status", habit_id=habit_id, days=90)

    assert "over 30 days" in status.content
    assert "Consider adding a minimum version" in status.content
    assert created.content == "Created habit 'Stretching'."


@pytest.mark.asyncio
async def test_get_habit_setup_lists_only_habit_linked_checkins(
    habit_db, invoke_tool, tool_context
) -> None:
    plugin = HabitsPlugin()
    await invoke_tool(
        plugin,
        "create_habit",
        name="Consistent Sleep",
        behavior="off screens by 11 PM, asleep by midnight",
        cue="11 PM",
    )
    habit_id = habit_db[store.HABITS_COLLECTION].docs[0]["id"]
    due_at = datetime(2026, 6, 14, 9, 0, tzinfo=timezone.utc)
    habit_db[store.HABIT_CHECKIN_PLANS_COLLECTION].docs.extend(
        [
            {
                "id": "hchk-habit",
                "owner_id": "owner-1",
                "habit_id": habit_id,
                "checkin_kind": "review",
                "message": "How did Consistent Sleep go?",
                "when": "09:00",
                "timezone": "UTC",
                "recurrence": "daily",
                "rule_id": "rule-habit",
                "initial_instance_id": "trg-habit",
                "active": True,
                "created_at": due_at,
                "updated_at": due_at,
            }
        ]
    )
    habit_db["trigger_rules"].docs.extend(
        [
            {
                "id": "rule-habit",
                "owner_id": "owner-1",
                "enabled": True,
                "updated_at": due_at,
                "origin": {"recurrence": "daily"},
                "action": {
                    "message": "How did Consistent Sleep go?",
                    "reply_grounding": {"habit_name": "Consistent Sleep"},
                },
                "management": {"provider": "habits", "resource_id": "hchk-habit"},
            },
            {
                "id": "rule-generic",
                "owner_id": "owner-1",
                "enabled": True,
                "updated_at": due_at,
                "origin": {"recurrence": "daily"},
                "action": {
                    "message": "Time to wind down and get off screens.",
                    "reply_grounding": {},
                },
                "management": {"provider": "scheduler", "resource_id": "rule-generic"},
            },
        ]
    )
    habit_db["trigger_instances"].docs.extend(
        [
            {
                "id": "trg-habit",
                "rule_id": "rule-habit",
                "owner_id": "owner-1",
                "status": "pending",
                "due_at": due_at,
                "action_snapshot": {
                    "message": "How did Consistent Sleep go?",
                    "reply_grounding": {"habit_name": "Consistent Sleep"},
                },
                "management": {"provider": "habits", "resource_id": "hchk-habit"},
            },
            {
                "id": "trg-generic",
                "rule_id": "rule-generic",
                "owner_id": "owner-1",
                "status": "pending",
                "due_at": due_at,
                "action_snapshot": {
                    "message": "Time to wind down and get off screens.",
                    "reply_grounding": {},
                },
                "management": {"provider": "scheduler", "resource_id": "trg-generic"},
            },
        ]
    )

    setup = await invoke_tool(plugin, "get_habit_setup", habit_id="Consistent Sleep")

    assert "Linked check-ins: 1 (review)." in setup.content
    assert "review" in setup.content
    assert "Time to wind down" not in setup.content

    checkins = await invoke_tool(
        plugin,
        "list_habit_checkins",
        habit_id="Consistent Sleep",
    )

    assert "review: pending, daily" in checkins.content


@pytest.mark.asyncio
async def test_schedule_habit_checkin_persists_plan_and_materializes_trigger(
    habit_db, invoke_tool, tool_context, monkeypatch
) -> None:
    trigger_time = datetime(2026, 6, 14, 9, 0, tzinfo=timezone.utc)
    create_rule = AsyncMock(return_value=SimpleNamespace(id="rule-1"))
    create_instance = AsyncMock(return_value=SimpleNamespace(id="trg-1"))
    monkeypatch.setattr(
        "plugins.habits.triggers.parse_schedule_time",
        lambda *_, **__: trigger_time,
    )
    monkeypatch.setattr(
        "plugins.habits.triggers.trigger_service",
        SimpleNamespace(create_rule=create_rule, create_instance=create_instance),
    )

    plugin = HabitsPlugin()
    await invoke_tool(
        plugin,
        "create_habit",
        name="Reading",
        behavior="read after dinner",
        cue="after dinner",
    )

    scheduled = await invoke_tool(
        plugin,
        "schedule_habit_checkin",
        habit_id="Reading",
        when="09:00",
        recurrence="daily",
        checkin_kind="review",
        instructions="Ask only if this would still be useful now.",
        decision="offer",
    )

    assert scheduled.content.startswith("Scheduled habit check-in for Reading at 9:00 AM")
    plan = habit_db[store.HABIT_CHECKIN_PLANS_COLLECTION].docs[0]
    assert plan["habit_id"] == habit_db[store.HABITS_COLLECTION].docs[0]["id"]
    assert plan["checkin_kind"] == "review"
    assert plan["decision"] == "offer"
    assert plan["rule_id"] == "rule-1"
    assert plan["initial_instance_id"] == "trg-1"
    rule_kwargs = create_rule.await_args.kwargs
    assert rule_kwargs["action"].decision == "offer"
    assert rule_kwargs["management"].resource_id == plan["id"]


@pytest.mark.asyncio
async def test_replace_habit_checkin_updates_plan_and_linked_trigger(
    habit_db, invoke_tool, tool_context, monkeypatch
) -> None:
    trigger_time = datetime(2026, 6, 15, 10, 30, tzinfo=timezone.utc)
    habit = {
        "id": "hab-1",
        "owner_id": "owner-1",
        "name": "Reading",
        "name_key": "reading",
        "behavior": "read after dinner",
        "active": True,
        "created_at": trigger_time,
        "updated_at": trigger_time,
    }
    plan = {
        "id": "hchk-1",
        "owner_id": "owner-1",
        "habit_id": "hab-1",
        "checkin_kind": "review",
        "message": "How did Reading go?",
        "when": "09:00",
        "timezone": "UTC",
        "recurrence": "daily",
        "decision": "tell",
        "rule_id": "rule-1",
        "initial_instance_id": "trg-1",
        "active": True,
        "created_at": trigger_time,
        "updated_at": trigger_time,
    }
    habit_db[store.HABITS_COLLECTION].docs.append(habit)
    habit_db[store.HABIT_CHECKIN_PLANS_COLLECTION].docs.append(plan)
    habit_db.trigger_rules.docs.append({
        "id": "rule-1",
        "owner_id": "owner-1",
        "origin": {},
        "action": {},
    })
    habit_db.trigger_instances.docs.append({
        "id": "trg-1",
        "owner_id": "owner-1",
        "rule_id": "rule-1",
        "status": "pending",
    })
    monkeypatch.setattr(
        "core.scheduling.parse_schedule_time",
        lambda *_, **__: trigger_time,
    )

    result = await invoke_tool(
        HabitsPlugin(),
        "replace_habit_checkin",
        plan_id="hchk-1",
        when="10:30",
        message="How was your reading?",
    )

    assert result.content == "Updated habit check-in for Reading."
    assert len(habit_db[store.HABIT_CHECKIN_PLANS_COLLECTION].docs) == 1
    assert plan["when"] == "10:30"
    assert plan["message"] == "How was your reading?"
    assert plan["recurrence"] == "daily"
    assert habit_db.trigger_rules.docs[0]["action"]["message"] == "How was your reading?"
    assert habit_db.trigger_instances.docs[0]["due_at"] == trigger_time


def test_habits_plugin_exposes_v0_tools() -> None:
    tools = HabitsPlugin().get_tools()

    assert set(tools) == {
        "create_habit",
        "get_habit_setup",
        "log_habit",
        "log_habit_by_name",
        "log_measured_habit_by_name",
        "get_habit_status",
        "list_habit_checkins",
        "schedule_habit_checkin",
        "replace_habit_checkin",
        "pause_habit_checkin",
        "resume_habit_checkin",
        "delete_habit_checkin",
    }
