"""Unit tests for OutlookProvider's Microsoft Graph event-shape conversion.

Covers the pure parsing functions in plugins.calendar.providers.outlook —
Graph event shape → CalendarEvent — for the cases that matter most:
- All-day events (date-only shape matches Google)
- Attendees list extraction
- Teams online meeting link → meet_link
- Cancelled + declined events filtered out
- Duration computation from start/end
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.calendar.providers.outlook import (
    OutlookProvider,
    _build_graph_dt,
    _is_graph_relevant,
    _parse_graph_datetime,
    _parse_graph_event,
)


class TestParseGraphEvent:
    def test_timed_event(self):
        item = {
            "id": "abc",
            "subject": "Standup",
            "isAllDay": False,
            "start": {"dateTime": "2026-05-01T09:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-01T09:30:00.0000000", "timeZone": "UTC"},
            "attendees": [],
        }
        event = _parse_graph_event(item)
        assert event.id == "abc"
        assert event.title == "Standup"
        assert event.is_all_day is False
        assert event.start.startswith("2026-05-01T09:00:00")
        assert event.duration_minutes == 30

    def test_recurring_occurrence_exposes_series_scope(self):
        event = _parse_graph_event({
            "id": "instance-1",
            "seriesMasterId": "series-1",
            "type": "occurrence",
            "subject": "Standup",
            "isAllDay": False,
            "start": {"dateTime": "2026-05-01T09:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-01T09:30:00", "timeZone": "UTC"},
        })

        assert event.scope == "occurrence"
        assert event.series_id == "series-1"

    def test_all_day_event_returns_date_only(self):
        item = {
            "id": "d1",
            "subject": "Holiday",
            "isAllDay": True,
            "start": {"dateTime": "2026-07-04T00:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2026-07-05T00:00:00.0000000", "timeZone": "UTC"},
        }
        event = _parse_graph_event(item)
        assert event.is_all_day is True
        assert event.start == "2026-07-04"
        assert event.end == "2026-07-05"
        assert event.duration_minutes is None

    def test_attendees_extracted_from_graph_shape(self):
        item = {
            "id": "m1",
            "subject": "Review",
            "isAllDay": False,
            "start": {"dateTime": "2026-05-01T10:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-01T11:00:00", "timeZone": "UTC"},
            "attendees": [
                {"emailAddress": {"address": "alice@example.com", "name": "Alice"}, "type": "required"},
                {"emailAddress": {"address": "bob@example.com", "name": "Bob"}, "type": "optional"},
            ],
        }
        event = _parse_graph_event(item)
        assert event.attendees == ["alice@example.com", "bob@example.com"]
        assert event.attendee_count == 2

    def test_teams_meeting_link_extracted(self):
        item = {
            "id": "t1",
            "subject": "Team Sync",
            "isAllDay": False,
            "start": {"dateTime": "2026-05-01T10:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-01T11:00:00", "timeZone": "UTC"},
            "onlineMeeting": {
                "joinUrl": "https://teams.microsoft.com/l/meetup-join/xyz",
            },
        }
        event = _parse_graph_event(item)
        assert event.meet_link == "https://teams.microsoft.com/l/meetup-join/xyz"

    def test_location_display_name(self):
        item = {
            "id": "l1",
            "subject": "Meeting",
            "isAllDay": False,
            "start": {"dateTime": "2026-05-01T10:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-01T11:00:00", "timeZone": "UTC"},
            "location": {"displayName": "Conference Room A"},
        }
        event = _parse_graph_event(item)
        assert event.location == "Conference Room A"

    def test_missing_subject_falls_back(self):
        item = {
            "id": "x",
            "subject": "",
            "isAllDay": False,
            "start": {"dateTime": "2026-05-01T10:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-05-01T11:00:00", "timeZone": "UTC"},
        }
        event = _parse_graph_event(item)
        assert event.title == "(No title)"


class TestIsGraphRelevant:
    def test_non_cancelled_accepted(self):
        assert _is_graph_relevant({"isCancelled": False}) is True

    def test_cancelled_rejected(self):
        assert _is_graph_relevant({"isCancelled": True}) is False

    def test_self_declined_rejected(self):
        assert _is_graph_relevant({"responseStatus": {"response": "declined"}}) is False

    def test_self_accepted_accepted(self):
        assert _is_graph_relevant({"responseStatus": {"response": "accepted"}}) is True


class TestParseGraphDatetime:
    def test_naive_treated_as_utc(self):
        out = _parse_graph_datetime({"dateTime": "2026-05-01T10:00:00", "timeZone": "UTC"})
        assert out == "2026-05-01T10:00:00+00:00"

    def test_offset_preserved(self):
        out = _parse_graph_datetime({"dateTime": "2026-05-01T10:00:00+02:00", "timeZone": "Europe/Berlin"})
        assert out == "2026-05-01T10:00:00+02:00"

    def test_empty_returns_empty(self):
        assert _parse_graph_datetime({}) == ""


class TestBuildGraphDt:
    def test_same_zone_strips_offset(self):
        # Offset-aware input in the same zone — just strips the offset.
        out = _build_graph_dt("2026-05-01T10:00:00+02:00", "Europe/Berlin")
        assert out == {"dateTime": "2026-05-01T10:00:00", "timeZone": "Europe/Berlin"}

    def test_converts_utc_z_to_target_zone(self):
        # 10:00 UTC → 06:00 New York (EDT in May)
        out = _build_graph_dt("2026-05-01T10:00:00Z", "America/New_York")
        assert out == {"dateTime": "2026-05-01T06:00:00", "timeZone": "America/New_York"}

    def test_defaults_to_utc_when_no_tz_name(self):
        # Z-suffix input, no tz_name → UTC stays UTC
        out = _build_graph_dt("2026-05-01T10:00:00Z", None)
        assert out == {"dateTime": "2026-05-01T10:00:00", "timeZone": "UTC"}


class _GraphResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


@pytest.mark.asyncio
async def test_update_event_start_only_preserves_existing_duration():
    existing = _GraphResponse({
        "start": {"dateTime": "2026-05-01T09:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-01T10:30:00", "timeZone": "UTC"},
    })
    patched = _GraphResponse({
        "id": "evt-1",
        "subject": "Moved",
        "start": {"dateTime": "2026-05-01T11:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-05-01T12:30:00", "timeZone": "UTC"},
    })
    client = SimpleNamespace(
        get=AsyncMock(return_value=existing),
        patch=AsyncMock(return_value=patched),
    )
    provider = OutlookProvider(client)

    await provider.update_event("evt-1", start="2026-05-01T11:00:00+00:00")

    patch_body = client.patch.await_args.kwargs["json"]
    assert patch_body["end"]["dateTime"] == "2026-05-01T12:30:00"


@pytest.mark.asyncio
async def test_create_event_yearly_recurrence_payload_and_confirmation():
    created = _GraphResponse({
        "id": "series-1",
        "subject": "Annual review",
        "isAllDay": True,
        "type": "seriesMaster",
        "start": {"dateTime": "2026-03-20T00:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-03-21T00:00:00", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {
                "type": "absoluteYearly",
                "interval": 1,
                "month": 3,
                "dayOfMonth": 20,
            },
            "range": {"type": "noEnd", "startDate": "2026-03-20"},
        },
    })
    client = SimpleNamespace(post=AsyncMock(return_value=created))
    provider = OutlookProvider(client)

    confirmation = await provider.create_event(
        title="Annual review",
        start="2026-03-20",
        all_day=True,
        recurrence="yearly",
        tz_name="UTC",
    )

    body = client.post.await_args.kwargs["json"]
    assert body["recurrence"]["pattern"]["type"] == "absoluteYearly"
    assert body["recurrence"]["pattern"]["month"] == 3
    assert body["recurrence"]["pattern"]["dayOfMonth"] == 20
    assert body["recurrence"]["range"]["type"] == "noEnd"
    assert confirmation.recurrence == "yearly"
    assert confirmation.scope == "series"


@pytest.mark.asyncio
async def test_update_event_yearly_recurrence_payload():
    existing = _GraphResponse({
        "start": {"dateTime": "2026-03-20T00:00:00", "timeZone": "UTC"},
        "isAllDay": True,
    })
    patched = _GraphResponse({
        "id": "evt-1",
        "subject": "Annual review",
        "isAllDay": True,
        "type": "seriesMaster",
        "start": {"dateTime": "2026-03-20T00:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-03-21T00:00:00", "timeZone": "UTC"},
        "recurrence": {
            "pattern": {
                "type": "absoluteYearly",
                "interval": 1,
                "month": 3,
                "dayOfMonth": 20,
            },
            "range": {"type": "noEnd", "startDate": "2026-03-20"},
        },
    })
    client = SimpleNamespace(
        get=AsyncMock(return_value=existing),
        patch=AsyncMock(return_value=patched),
    )
    provider = OutlookProvider(client)

    confirmation = await provider.update_event(
        "evt-1",
        recurrence="yearly",
        tz_name="UTC",
    )

    patch_body = client.patch.await_args.kwargs["json"]
    assert patch_body["recurrence"]["pattern"]["type"] == "absoluteYearly"
    assert confirmation.recurrence == "yearly"
    assert confirmation.scope == "series"
