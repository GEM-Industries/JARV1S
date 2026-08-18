from unittest.mock import AsyncMock

import pytest

from plugins.calendar.providers.google import GoogleProvider, _parse_event


class _GoogleResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_parse_recurring_occurrence_exposes_series_scope():
    event = _parse_event({
        "id": "instance-1",
        "recurringEventId": "series-1",
        "summary": "Standup",
        "start": {"dateTime": "2026-05-01T09:00:00+00:00"},
        "end": {"dateTime": "2026-05-01T09:30:00+00:00"},
    })

    assert event.scope == "occurrence"
    assert event.series_id == "series-1"


@pytest.mark.asyncio
async def test_update_event_start_only_preserves_existing_duration():
    provider = GoogleProvider(client=object())
    provider._request = AsyncMock(side_effect=[
        _GoogleResponse({
            "start": {"dateTime": "2026-05-01T09:00:00+00:00"},
            "end": {"dateTime": "2026-05-01T10:30:00+00:00"},
        }),
        _GoogleResponse({
            "id": "evt-1",
            "summary": "Moved",
            "start": {"dateTime": "2026-05-01T11:00:00+00:00"},
            "end": {"dateTime": "2026-05-01T12:30:00+00:00"},
        }),
    ])

    await provider.update_event("evt-1", start="2026-05-01T11:00:00+00:00")

    patch_body = provider._request.await_args_list[1].kwargs["json"]
    assert patch_body["end"]["dateTime"] == "2026-05-01T12:30:00+00:00"


@pytest.mark.asyncio
async def test_create_event_yearly_recurrence_payload_and_confirmation():
    provider = GoogleProvider(client=object(), account="personal")
    provider._request = AsyncMock(return_value=_GoogleResponse({
        "id": "series-1",
        "summary": "Annual review",
        "start": {"date": "2026-03-20"},
        "end": {"date": "2026-03-21"},
        "recurrence": ["RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20"],
    }))

    confirmation = await provider.create_event(
        title="Annual review",
        start="2026-03-20",
        all_day=True,
        recurrence="yearly",
    )

    body = provider._request.await_args.kwargs["json"]
    assert body["recurrence"] == ["RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20"]
    assert confirmation.recurrence == "yearly"
    assert confirmation.scope == "series"


@pytest.mark.asyncio
async def test_update_event_yearly_recurrence_payload():
    provider = GoogleProvider(client=object(), account="personal")
    provider._request = AsyncMock(side_effect=[
        _GoogleResponse({
            "start": {"date": "2026-03-20"},
            "end": {"date": "2026-03-21"},
        }),
        _GoogleResponse({
            "id": "evt-1",
            "summary": "Annual review",
            "start": {"date": "2026-03-20"},
            "end": {"date": "2026-03-21"},
            "recurrence": ["RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20"],
        }),
    ])

    confirmation = await provider.update_event("evt-1", recurrence="yearly")

    patch_body = provider._request.await_args_list[1].kwargs["json"]
    assert patch_body["recurrence"] == ["RRULE:FREQ=YEARLY;BYMONTH=3;BYMONTHDAY=20"]
    assert confirmation.recurrence == "yearly"
    assert confirmation.scope == "series"
