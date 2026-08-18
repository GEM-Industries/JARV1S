from datetime import datetime, timezone

import pytest

from plugins import time as time_plugin
from plugins.time import TimePlugin


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 6, 13, 6, 0, 54, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_time_in_reuses_shared_local_datetime_format(monkeypatch, tool_context):
    monkeypatch.setattr(time_plugin, "datetime", FixedDatetime)

    with tool_context(owner_id="geoff", timezone="Australia/Sydney"):
        result = await TimePlugin().time_in("London")

    assert result.timezone == "Europe/London"
    assert result.datetime == "2026-06-13T07:00:54+01:00"
    assert result.readable == "7:00 AM"
    assert result.offset == "UTC+01:00"
