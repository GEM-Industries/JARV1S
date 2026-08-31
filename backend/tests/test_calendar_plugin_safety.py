from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from plugins.calendar import CalendarPlugin
from plugins.calendar.models import CalendarEvent, CalendarQueryResult, EventConfirmation
from core.plugins.capabilities import CapabilityErrorDetail


def _event(
    *,
    id: str = "evt-1",
    title: str = "LinkedIn coffee with Jerry",
    start: str = "2026-05-27T14:00:00+00:00",
    end: str = "2026-05-27T15:00:00+00:00",
    account: str = "google",
    scope: str = "event",
    series_id: str | None = None,
) -> CalendarEvent:
    return CalendarEvent(
        id=id,
        title=title,
        start=start,
        end=end,
        account=account,
        scope=scope,
        series_id=series_id,
    )


class _FakeCalendar:
    def __init__(self, event: CalendarEvent | None = None):
        self.event = event
        self.get_event = AsyncMock(side_effect=self._get_event)
        self.delete_event = AsyncMock(return_value="Deleted.")
        self.update_event = AsyncMock(return_value=EventConfirmation(
            id="evt-1",
            title="Updated event",
            start="2026-05-27T15:00:00+00:00",
            end="2026-05-27T16:00:00+00:00",
            account="google",
        ))
        events = [event] if event else []
        self.list_events = AsyncMock(return_value=CalendarQueryResult(
            events=events,
            time_min="2026-05-27T00:00:00+00:00",
            time_max="2026-05-28T00:00:00+00:00",
            match_status="single" if events else "none",
            coverage="complete",
        ))
        self.search_events = AsyncMock(return_value=CalendarQueryResult(
            events=events,
            time_min="2026-01-01T00:00:00+00:00",
            time_max="2027-01-01T00:00:00+00:00",
            query="Charlie",
            match_status="single" if events else "none",
            coverage="complete",
        ))
        self.create_event = AsyncMock(return_value=EventConfirmation(
            id="new-evt",
            title="New event",
            start="2026-05-27T14:00:00+00:00",
            end="2026-05-27T15:00:00+00:00",
            account="google",
        ))

    async def _get_event(self, event_id: str, account: str | None = None) -> CalendarEvent:
        if self.event is None:
            raise RuntimeError("not found")
        return self.event


@pytest.mark.asyncio
async def test_delete_event_fetches_target_before_approval(monkeypatch):
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event())
    captured: dict = {}

    async def fake_require_consent(description, action, detail=""):
        captured["description"] = description
        captured["detail"] = detail
        captured["result"] = await action()
        return CapabilityErrorDetail(
            code="approval_needed",
            message=f"Approval needed: {description} The action has not executed yet.",
        )

    monkeypatch.setattr("plugins.calendar.require_consent", fake_require_consent)

    result = await plugin.delete_event(
        event_id="evt-1",
        account="google",
        expected_title="coffee",
        calendar=fake_calendar,
    )

    assert result.code == "approval_needed"
    assert "LinkedIn coffee with Jerry" in captured["description"]
    assert "evt-1" in captured["detail"]
    assert "Account: google" in captured["detail"]
    assert captured["result"].content == 'Deleted "LinkedIn coffee with Jerry".'
    fake_calendar.get_event.assert_awaited_once_with("evt-1", account="google")
    fake_calendar.delete_event.assert_awaited_once_with("evt-1", account="google")


@pytest.mark.asyncio
async def test_delete_event_missing_target_does_not_create_approval(monkeypatch):
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(None)

    async def fail_require_consent(*_args, **_kwargs):
        raise AssertionError("approval should not be created")

    monkeypatch.setattr("plugins.calendar.require_consent", fail_require_consent)

    result = await plugin.delete_event(
        event_id="missing",
        account="google",
        calendar=fake_calendar,
    )

    assert result.message.startswith("Could not resolve calendar event")
    fake_calendar.delete_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_event_expected_title_mismatch_refuses(monkeypatch):
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(title="Knowledge and Belief, Lecture"))

    async def fail_require_consent(*_args, **_kwargs):
        raise AssertionError("approval should not be created")

    monkeypatch.setattr("plugins.calendar.require_consent", fail_require_consent)

    result = await plugin.delete_event(
        event_id="lecture",
        account="google",
        expected_title="Jerry",
        calendar=fake_calendar,
    )

    assert result.message.startswith("Refusing to delete; target title did not match.")
    fake_calendar.delete_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_event_provider_not_found_result_is_error(monkeypatch):
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event())
    fake_calendar.delete_event.return_value = CapabilityErrorDetail(
        code="not_found",
        message="Event 'evt-1' not found.",
    )
    captured: dict = {}

    async def fake_require_consent(description, action, detail=""):
        captured["result"] = await action()
        return CapabilityErrorDetail(
            code="approval_needed",
            message=f"Approval needed: {description} The action has not executed yet.",
        )

    monkeypatch.setattr("plugins.calendar.require_consent", fake_require_consent)

    await plugin.delete_event(
        event_id="evt-1",
        account="google",
        calendar=fake_calendar,
    )

    assert captured["result"].code == "not_found"
    assert captured["result"].message == "Event 'evt-1' not found."


@pytest.mark.asyncio
async def test_create_event_exact_duplicate_returns_existing_event():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(title="Quiet Mode Test"))

    result = await plugin.create_event(
        title="Quiet Mode Test",
        start="2026-05-27T14:00:00+00:00",
        end="2026-05-27T15:00:00+00:00",
        account="google",
        calendar=fake_calendar,
    )

    assert result.id == "evt-1"
    assert result.title == "Quiet Mode Test"
    assert result.conflicts[0].id == "evt-1"
    fake_calendar.create_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_event_same_title_overlap_returns_existing_event():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(
        title="Quiet Mode Test",
        start="2026-05-27T14:00:00+00:00",
        end="2026-05-27T14:15:00+00:00",
    ))

    result = await plugin.create_event(
        title="Quiet Mode Test",
        start="2026-05-27T14:05:00+00:00",
        duration_minutes=15,
        account="google",
        calendar=fake_calendar,
    )

    assert result.id == "evt-1"
    assert result.start == "2026-05-27T14:00:00+00:00"
    fake_calendar.create_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_event_non_duplicate_still_creates():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(title="Different meeting"))

    result = await plugin.create_event(
        title="Quiet Mode Test",
        start="2026-05-27T14:00:00+00:00",
        end="2026-05-27T15:00:00+00:00",
        account="google",
        calendar=fake_calendar,
    )

    assert result.id == "new-evt"
    fake_calendar.create_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_event_normalizes_natural_start_and_duration_string(monkeypatch):
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar()
    monkeypatch.setattr("plugins.calendar._tz", lambda: ZoneInfo("UTC"))

    await plugin.create_event(
        title="Coffee",
        start="May 27 2027 at 2pm",
        duration_minutes="90m",
        account="google",
        calendar=fake_calendar,
    )

    fake_calendar.create_event.assert_awaited_once()
    kwargs = fake_calendar.create_event.await_args.kwargs
    assert kwargs["start"] == "2027-05-27T14:00:00+00:00"
    assert kwargs["duration_minutes"] == 90


@pytest.mark.asyncio
async def test_update_event_expected_title_mismatch_refuses():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(title="Quiet Mode Test"))

    result = await plugin.update_event(
        event_id="evt-1",
        start="2026-05-27T15:00:00+00:00",
        expected_title="Test Alert Silent Mode",
        account="google",
        calendar=fake_calendar,
    )

    assert result.message.startswith("Refusing to update; target title did not match.")
    fake_calendar.update_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_event_expected_target_updates_when_matched():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(title="Quiet Mode Test"))

    result = await plugin.update_event(
        event_id="evt-1",
        start="2026-05-27T15:00:00+00:00",
        expected_title="Quiet Mode",
        expected_account="google",
        account="google",
        calendar=fake_calendar,
    )

    assert result.id == "evt-1"
    fake_calendar.update_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_event_always_fetches_target_before_write():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event())

    result = await plugin.update_event(
        event_id="evt-1",
        start="2026-05-27T15:00:00+00:00",
        account="google",
        calendar=fake_calendar,
    )

    assert result.id == "evt-1"
    fake_calendar.get_event.assert_awaited_once_with("evt-1", account="google")
    fake_calendar.update_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_recurring_event_requires_explicit_scope():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(
        scope="occurrence",
        series_id="series-1",
    ))

    result = await plugin.update_event(
        event_id="evt-1",
        start="2026-05-27T15:00:00+00:00",
        account="google",
        calendar=fake_calendar,
    )

    assert isinstance(result, CapabilityErrorDetail)
    assert "scope='occurrence'" in result.message
    fake_calendar.update_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_recurring_series_uses_series_id():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(
        scope="occurrence",
        series_id="series-1",
    ))

    result = await plugin.update_event(
        event_id="evt-1",
        title="Updated series",
        scope="series",
        account="google",
        calendar=fake_calendar,
    )

    assert result.scope == "series"
    assert fake_calendar.update_event.await_args.kwargs["event_id"] == "series-1"


@pytest.mark.asyncio
async def test_search_events_defaults_to_calendar_year(tool_context, monkeypatch):
    """Existence queries (birthdays) must not default to next-90-days agenda window."""
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar()
    fixed_now = datetime(2026, 8, 11, 15, 0, tzinfo=ZoneInfo("Australia/Sydney"))

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr("plugins.calendar.datetime", _FixedDateTime)

    with tool_context(timezone="Australia/Sydney"):
        await plugin.search_events(query="Charlie birthday", calendar=fake_calendar)

    time_min, time_max = fake_calendar.search_events.await_args.args[1:3]
    assert time_min.startswith("2026-01-01T00:00:00")
    assert time_max.startswith("2027-01-01T00:00:00")


@pytest.mark.asyncio
async def test_get_next_event_looks_past_all_day_clutter(tool_context, monkeypatch):
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event())
    fixed_now = datetime(2026, 8, 11, 15, 0, tzinfo=ZoneInfo("Australia/Sydney"))

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr("plugins.calendar.datetime", _FixedDateTime)

    with tool_context(timezone="Australia/Sydney"):
        await plugin.get_next_event(calendar=fake_calendar)

    assert fake_calendar.list_events.await_args.kwargs["max_results"] == 200


@pytest.mark.asyncio
async def test_search_events_start_only_keeps_bounded_forward_window(tool_context, monkeypatch):
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar()
    fixed_now = datetime(2026, 8, 11, 15, 0, tzinfo=ZoneInfo("Australia/Sydney"))

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fixed_now.replace(tzinfo=None)
            return fixed_now.astimezone(tz)

    monkeypatch.setattr("plugins.calendar.datetime", _FixedDateTime)

    with tool_context(timezone="Australia/Sydney"):
        await plugin.search_events(
            query="dentist",
            start_date="2026-08-01",
            calendar=fake_calendar,
        )

    time_min, time_max = fake_calendar.search_events.await_args.args[1:3]
    assert time_min.startswith("2026-08-01T00:00:00")
    assert time_max.startswith("2026-10-30T00:00:00")


@pytest.mark.asyncio
async def test_search_events_partial_empty_fails_closed():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar()
    fake_calendar.search_events.return_value = CalendarQueryResult(
        events=[],
        time_min="2026-01-01T00:00:00+00:00",
        time_max="2027-01-01T00:00:00+00:00",
        query="Mum birthday",
        match_status="none",
        coverage="partial",
        truncated=True,
        failed_providers=[],
    )

    result = await plugin.search_events(query="Mum birthday", calendar=fake_calendar)

    assert isinstance(result, CapabilityErrorDetail)
    assert result.message.startswith("Could not confirm that no matching events exist")
    assert "partial" in result.message


@pytest.mark.asyncio
async def test_create_event_passes_recurrence():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar()
    fake_calendar.create_event.return_value = EventConfirmation(
        id="series-1",
        title="Annual review",
        start="2026-03-20",
        end="2026-03-21",
        account="google",
        scope="series",
        series_id="series-1",
        recurrence="yearly",
    )

    result = await plugin.create_event(
        title="Annual review",
        start="2026-03-20",
        all_day=True,
        recurrence="yearly",
        account="google",
        calendar=fake_calendar,
    )

    assert result.recurrence == "yearly"
    assert result.scope == "series"
    assert fake_calendar.create_event.await_args.kwargs["recurrence"] == "yearly"


@pytest.mark.asyncio
async def test_create_all_day_duplicate_returns_existing_event():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(
        title="Charlotte Birthday",
        start="2026-03-20",
        end="2026-03-21",
    ))

    result = await plugin.create_event(
        title="Charlotte Birthday",
        start="2026-03-20",
        all_day=True,
        account="google",
        calendar=fake_calendar,
    )

    assert result.id == "evt-1"
    fake_calendar.create_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_all_day_conflict_check_uses_rfc3339_window(tool_context, monkeypatch):
    """Google rejects date-only timeMin/timeMax; all-day creates must list with datetimes."""
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar()
    monkeypatch.setattr("plugins.calendar._tz", lambda: ZoneInfo("Australia/Sydney"))

    with tool_context(timezone="Australia/Sydney"):
        await plugin.create_event(
            title="Annual review",
            start="2026-03-20",
            all_day=True,
            recurrence="yearly",
            account="google",
            calendar=fake_calendar,
        )

    time_min, time_max = fake_calendar.list_events.await_args.args[:2]
    assert "T" in time_min
    assert "T" in time_max
    assert time_min.startswith("2026-03-20T00:00:00")
    assert time_max.startswith("2026-03-21T00:00:00")
    assert fake_calendar.create_event.await_args.kwargs["recurrence"] == "yearly"


@pytest.mark.asyncio
async def test_update_event_converts_one_off_with_recurrence():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(
        title="Charlotte Birthday",
        start="2026-03-20",
        end="2026-03-21",
    ))
    fake_calendar.update_event.return_value = EventConfirmation(
        id="evt-1",
        title="Charlotte Birthday",
        start="2026-03-20",
        end="2026-03-21",
        account="google",
        scope="series",
        series_id="evt-1",
        recurrence="yearly",
    )

    result = await plugin.update_event(
        event_id="evt-1",
        recurrence="yearly",
        account="google",
        calendar=fake_calendar,
    )

    assert result.recurrence == "yearly"
    assert result.scope == "series"
    assert fake_calendar.update_event.await_args.kwargs["recurrence"] == "yearly"
    assert fake_calendar.update_event.await_args.kwargs["event_id"] == "evt-1"


@pytest.mark.asyncio
async def test_update_occurrence_recurrence_requires_series_scope():
    plugin = CalendarPlugin()
    fake_calendar = _FakeCalendar(_event(
        scope="occurrence",
        series_id="series-1",
    ))

    result = await plugin.update_event(
        event_id="evt-1",
        recurrence="yearly",
        account="google",
        calendar=fake_calendar,
    )

    assert isinstance(result, CapabilityErrorDetail)
    assert "scope='series'" in result.message
    fake_calendar.update_event.assert_not_awaited()
