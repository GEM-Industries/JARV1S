from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta, timezone

import pytest

from core.activity.headless import headless_rows_to_activity_items
from core.activity.models import ActivityDetailRef, ActivityEntry, ActivityItem
from core.activity.page import ActivityQuery, _SourcePage, activity_page
from core.activity.service import recent_activity
import core.activity.service as activity_service


def test_headless_groups_by_turn_id_and_sets_delivery():
    rows = [
        {
            "timestamp": "2026-06-03T10:00:00+00:00",
            "turn_id": "turn-a",
            "delivery": "silent",
            "role": "assistant",
            "content": "Checked calendar.",
            "turn_type": "text_only",
            "trigger_source": "automation",
            "rule_name": "Morning check",
        },
        {
            "timestamp": "2026-06-03T10:00:01+00:00",
            "turn_id": "turn-a",
            "delivery": "silent",
            "role": "user",
            "content": "<tool_result>ok</tool_result>",
            "turn_type": "tool_result",
        },
    ]
    items = headless_rows_to_activity_items(rows)
    assert len(items) == 1
    assert items[0].kind == "headless"
    assert items[0].id == "turn-a"
    assert items[0].outcome == "completed"
    assert items[0].delivery == "silent"
    assert items[0].source == "Morning check"
    assert items[0].summary == "Checked calendar."
    assert items[0].trace is not None
    assert len(items[0].trace) == 2


def test_suppressed_vs_silent_delivery_axes():
    silent = headless_rows_to_activity_items([
        {
            "timestamp": "2026-06-03T11:00:00+00:00",
            "turn_id": "t1",
            "delivery": "silent",
            "role": "assistant",
            "content": "Ran protocol.",
            "protocol_name": "Startup",
        },
    ])
    suppressed = headless_rows_to_activity_items([
        {
            "timestamp": "2026-06-03T12:00:00+00:00",
            "turn_id": "t2",
            "delivery": "suppressed",
            "role": "assistant",
            "content": "NO_REPLY",
            "trigger_source": "system_pulse",
        },
    ])
    assert silent[0].delivery == "silent"
    assert suppressed[0].delivery == "suppressed"
    assert silent[0].outcome == "completed"
    assert suppressed[0].outcome == "completed"


@pytest.mark.asyncio
async def test_recent_activity_merges_and_sorts():
    task_item = ActivityItem(
        kind="task",
        id="task1",
        summary="Running research",
        when="2026-06-03T14:00:00+10:00",
        sort_at="2026-06-03T04:00:00+00:00",
        outcome="running",
    )
    trigger_item = ActivityItem(
        kind="trigger",
        id="inst-1",
        summary="Reminder waiting",
        when="2026-06-03T15:00:00+10:00",
        sort_at="2026-06-03T05:00:00+00:00",
        outcome="awaiting",
    )

    with (
        patch("core.activity.service._recent_headless_turns", AsyncMock(return_value=[])),
        patch("core.activity.service._recent_runs", AsyncMock(return_value=[trigger_item])),
        patch("core.activity.service._recent_tasks", AsyncMock(return_value=[task_item])),
    ):
        items = await recent_activity("owner-1", limit=10)

    assert [i.id for i in items] == ["inst-1", "task1"]


@pytest.mark.asyncio
async def test_recent_activity_kind_filter_tasks_only():
    task_item = ActivityItem(
        kind="task",
        id="task1",
        summary="Done",
        when="2026-06-03T10:00:00+10:00",
        sort_at="2026-06-03T00:00:00+00:00",
        outcome="completed",
    )

    with (
        patch("core.activity.service._recent_headless_turns", AsyncMock()) as headless,
        patch("core.activity.service._recent_runs", AsyncMock()) as runs,
        patch("core.activity.service._recent_tasks", AsyncMock(return_value=[task_item])) as tasks,
    ):
        items = await recent_activity("owner-1", limit=10, kind="task")

    headless.assert_not_called()
    runs.assert_not_called()
    tasks.assert_awaited_once()
    assert len(items) == 1
    assert items[0].kind == "task"


@pytest.mark.asyncio
async def test_recent_activity_kind_filter_headless_only():
    headless_item = ActivityItem(
        kind="headless",
        id="turn-1",
        summary="Quietly checked context",
        when="2026-06-03T10:00:00+10:00",
        sort_at="2026-06-03T00:00:00+00:00",
        outcome="completed",
        delivery="silent",
    )

    with (
        patch("core.activity.service._recent_headless_turns", AsyncMock(return_value=[headless_item])) as headless,
        patch("core.activity.service._recent_runs", AsyncMock()) as runs,
        patch("core.activity.service._recent_tasks", AsyncMock()) as tasks,
    ):
        items = await recent_activity("owner-1", limit=10, kind="headless")

    headless.assert_awaited_once_with("owner-1", 10)
    runs.assert_not_called()
    tasks.assert_not_called()
    assert [item.kind for item in items] == ["headless"]


def test_run_outcome_maps_in_flight_statuses_to_running():
    assert activity_service._run_outcome("claimed") == "running"
    assert activity_service._run_outcome("executing") == "running"


@pytest.mark.asyncio
async def test_recent_runs_projects_successful_automation_instances(monkeypatch):
    class FakeCursor:
        def __init__(self, docs):
            self.docs = docs

        def sort(self, *_args):
            return self

        def limit(self, *_args):
            return self

        async def to_list(self, *args, **kwargs):
            return self.docs

    class FakeCollection:
        def __init__(self, docs):
            self.docs = docs
            self.query = None

        def find(self, query):
            self.query = query
            return FakeCursor(self.docs)

    class FakeDB:
        def __init__(self, docs):
            self.trigger_instances = FakeCollection(docs)

    docs = [
        {
            "id": "trg-1",
            "owner_id": "owner-1",
            "status": "completed",
            "updated_at": datetime(2026, 6, 3, 5, tzinfo=timezone.utc),
            "origin_snapshot": {"kind": "external", "source": "calendar"},
            "action_snapshot": {"kind": "evaluate", "message": "Check calendar"},
            "source_event": {
                "rule_id": "rule-1",
                "rule_name": "Morning check",
                "item_id": "event-1",
            },
            "result_text": "Calendar checked.",
        },
    ]
    fake_db = FakeDB(docs)
    monkeypatch.setattr(activity_service, "mongodb", type("FakeMongo", (), {"db": fake_db})())

    items = await activity_service._recent_runs("owner-1", 10, kind="automation")

    assert fake_db.trigger_instances.query == {
        "owner_id": "owner-1",
        "status": {"$in": list(activity_service.RUN_STATUSES)},
        "origin_snapshot.kind": "external",
        "source_event.rule_id": {"$exists": True},
    }
    assert len(items) == 1
    assert items[0].kind == "automation"
    assert items[0].id == "trg-1"
    assert items[0].outcome == "completed"
    assert items[0].source == "Morning check"
    assert items[0].summary == "Calendar checked."


def test_humanize_failure_maps_known_and_unknown_codes():
    from core.triggers.vocabulary import humanize_failure_reason

    assert humanize_failure_reason("delivery_ttl_expired") == "Delivery window expired"
    assert humanize_failure_reason("calendar_event_started") == "Event already started"
    assert humanize_failure_reason("runtime_error") == "Language model unavailable"
    # Unknown codes degrade gracefully instead of leaking snake_case.
    assert humanize_failure_reason("some_new_reason") == "Some new reason"
    assert humanize_failure_reason("") is None
    assert humanize_failure_reason(None) is None


@pytest.mark.asyncio
async def test_recent_runs_surfaces_failure_label_without_polluting_summary(monkeypatch):
    class FakeCursor:
        def __init__(self, docs):
            self.docs = docs

        def sort(self, *_args):
            return self

        def limit(self, *_args):
            return self

        async def to_list(self, *args, **kwargs):
            return self.docs

    class FakeCollection:
        def find(self, query):
            return FakeCursor(self.docs)

        def __init__(self, docs):
            self.docs = docs

    fake_db = type("FakeDB", (), {})()
    fake_db.trigger_instances = FakeCollection([
        {
            "id": "trg-2",
            "owner_id": "owner-1",
            "status": "expired",
            "updated_at": datetime(2026, 6, 3, 6, tzinfo=timezone.utc),
            "origin_snapshot": {"kind": "time"},
            "action_snapshot": {"kind": "notify", "message": "Time to wind down."},
            "source_event": {},
            "failure_reason": "delivery_ttl_expired",
        },
    ])
    monkeypatch.setattr(activity_service, "mongodb", type("FakeMongo", (), {"db": fake_db})())

    items = await activity_service._recent_runs("owner-1", 10, kind="trigger")

    assert len(items) == 1
    assert items[0].outcome == "failed"
    assert items[0].summary == "Time to wind down."
    assert items[0].failure_label == "Delivery window expired"


@pytest.mark.asyncio
async def test_get_activity_route():
    from api.routes.activity import get_activity

    sample = [
        ActivityItem(
            kind="automation",
            id="r1:i1",
            summary="Automation failed",
            when="2026-06-03T09:00:00+10:00",
            sort_at="2026-06-02T23:00:00+00:00",
            outcome="failed",
            source="Inbox watcher",
        ),
    ]
    with patch("api.routes.activity.recent_activity", AsyncMock(return_value=sample)):
        result = await get_activity(limit=10, kind=None)
    assert result == sample


@pytest.mark.asyncio
async def test_activity_plugin_recent_accepts_headless_kind(monkeypatch):
    from plugins.activity import ActivityPlugin

    sample = [
        ActivityItem(
            kind="headless",
            id="turn-1",
            summary="Quiet turn",
            when="2026-06-03T09:00:00+10:00",
            sort_at="2026-06-02T23:00:00+00:00",
            outcome="completed",
            delivery="silent",
        ),
    ]
    recent = AsyncMock(return_value=sample)
    monkeypatch.setattr("plugins.activity.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr("plugins.activity.recent_activity", recent)

    result = await ActivityPlugin().recent(kind="headless", limit=100)

    assert result == sample
    recent.assert_awaited_once_with("owner-1", limit=100, kind="headless")


@pytest.mark.asyncio
async def test_activity_plugin_why_last_fire_reports_ambiguous_candidates(monkeypatch):
    from core.operations.projection import ManagedSetup
    from plugins.activity import ActivityPlugin

    candidates = [
        ManagedSetup(
            resource_ref="scheduler:schedule:a",
            resource_id="a",
            setup_type="schedule",
            managed_by="scheduler",
            kind="reminder",
            name="Standup",
            trigger_label="At 09:00",
        ),
        ManagedSetup(
            resource_ref="scheduler:schedule:b",
            resource_id="b",
            setup_type="schedule",
            managed_by="scheduler",
            kind="reminder",
            name="Standup prep",
            trigger_label="At 08:45",
        ),
    ]
    monkeypatch.setattr("plugins.activity.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(
        "plugins.activity.resolve_managed_setup",
        AsyncMock(return_value=candidates),
    )

    result = await ActivityPlugin().why_last_fire("standup")

    assert result.message.startswith("Ambiguous setup")
    assert "Standup (scheduler:schedule:a; At 09:00)" in result.message


@pytest.mark.asyncio
async def test_recent_activity_rejects_out_of_range_limit():
    with pytest.raises(ValueError, match="Activity limit"):
        await recent_activity("owner-1", limit=0)
    with pytest.raises(ValueError, match="Activity limit"):
        await recent_activity("owner-1", limit=201)


def _entry(category: str, ident: str, at: datetime) -> ActivityEntry:
    detail_kind = "background_task" if category == "task" else "turn"
    return ActivityEntry(
        activity_id=f"{category}:{ident}",
        category=category,
        occurred_at=at,
        outcome="succeeded",
        title=ident,
        detail_ref=ActivityDetailRef(kind=detail_kind, id=ident),
    )


@pytest.mark.asyncio
async def test_activity_page_all_excludes_conversations(monkeypatch):
    import core.activity.page as page_module

    at = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    source_items = {
        "conversation": [_entry("conversation", "chat", at)],
        "reminder": [_entry("reminder", "ping", at)],
        "automation": [],
        "task": [],
        "system": [],
    }

    def loader(source):
        async def load(_owner, _query, position, limit):
            items = source_items[source]
            return _SourcePage(source, items[:limit], len(items) > limit)
        return load

    monkeypatch.setattr(page_module, "_conversation_page", loader("conversation"))
    monkeypatch.setattr(
        page_module,
        "_run_page",
        lambda owner, query, position, limit, *, category: loader(category)(
            owner, query, position, limit
        ),
    )
    monkeypatch.setattr(page_module, "_task_page", loader("task"))
    monkeypatch.setattr(page_module, "_system_page", loader("system"))

    page = await activity_page("owner-1", query=ActivityQuery(), limit=10)
    assert [item.activity_id for item in page.items] == ["reminder:ping"]

    chats = await activity_page(
        "owner-1",
        query=ActivityQuery(category="conversation"),
        limit=10,
    )
    assert [item.activity_id for item in chats.items] == ["conversation:chat"]


@pytest.mark.asyncio
async def test_activity_page_cursor_is_stable_across_tied_timestamps(monkeypatch):
    import core.activity.page as page_module

    at = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    source_items = {
        "conversation": [],
        "reminder": [_entry("reminder", "z", at), _entry("reminder", "y", at)],
        "automation": [_entry("automation", "b", at)],
        "task": [],
        "system": [],
    }

    def loader(source):
        async def load(_owner, _query, position, limit):
            items = source_items[source]
            if position:
                items = [
                    item for item in items
                    if (item.occurred_at, item.activity_id)
                    < (position.occurred_at, position.activity_id)
                ]
            return _SourcePage(source, items[:limit], len(items) > limit)
        return load

    monkeypatch.setattr(page_module, "_conversation_page", loader("conversation"))
    monkeypatch.setattr(
        page_module,
        "_run_page",
        lambda owner, query, position, limit, *, category: loader(category)(
            owner, query, position, limit
        ),
    )
    monkeypatch.setattr(page_module, "_task_page", loader("task"))
    monkeypatch.setattr(page_module, "_system_page", loader("system"))

    first = await activity_page("owner-1", query=ActivityQuery(), limit=2)
    second = await activity_page(
        "owner-1",
        query=ActivityQuery(),
        cursor=first.next_cursor,
        limit=2,
    )

    assert [item.activity_id for item in first.items] == ["reminder:z", "reminder:y"]
    assert [item.activity_id for item in second.items] == ["automation:b"]
    assert not second.has_more


@pytest.mark.asyncio
async def test_activity_page_does_not_starve_quieter_sources(monkeypatch):
    import core.activity.page as page_module

    base = datetime(2026, 6, 3, 5, tzinfo=timezone.utc)
    source_items = {
        "conversation": [],
        "reminder": [
            _entry("reminder", str(index), base - timedelta(seconds=index))
            for index in range(5)
        ],
        "automation": [_entry("automation", "quiet", base - timedelta(seconds=3))],
        "task": [],
        "system": [],
    }

    def loader(source):
        async def load(_owner, _query, position, limit):
            items = source_items[source]
            if position:
                items = [
                    item for item in items
                    if (item.occurred_at, item.activity_id)
                    < (position.occurred_at, position.activity_id)
                ]
            return _SourcePage(source, items[:limit], len(items) > limit)
        return load

    monkeypatch.setattr(page_module, "_conversation_page", loader("conversation"))
    monkeypatch.setattr(
        page_module,
        "_run_page",
        lambda owner, query, position, limit, *, category: loader(category)(
            owner, query, position, limit
        ),
    )
    monkeypatch.setattr(page_module, "_task_page", loader("task"))
    monkeypatch.setattr(page_module, "_system_page", loader("system"))

    seen: list[str] = []
    cursor = None
    while True:
        page = await activity_page("owner-1", cursor=cursor, limit=2)
        seen.extend(item.activity_id for item in page.items)
        if not page.has_more:
            break
        cursor = page.next_cursor

    assert seen.count("automation:quiet") == 1
    assert len(seen) == len(set(seen)) == 6


@pytest.mark.asyncio
async def test_activity_page_rejects_cursor_reused_with_different_filters():
    import core.activity.page as page_module

    cursor = page_module._encode_cursor(
        page_module._Cursor(query=ActivityQuery(category="task").fingerprint())
    )
    with pytest.raises(ValueError, match="does not match"):
        await activity_page(
            "owner-1",
            query=ActivityQuery(category="automation"),
            cursor=cursor,
        )
