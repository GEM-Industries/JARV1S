"""Compatibility layer for scheduling imports.

Shared user-facing time parsing lives in core.time.parsing.
"""

from __future__ import annotations

from datetime import datetime, time

from core.time import (
    coerce_datetime,
    coerce_datetime_or_none,
    coerce_timezone,
    format_local_when,
    LocalDateTimeFields,
    local_datetime_fields,
    parse_calendar_date,
    parse_future_datetime,
)


def parse_schedule_time(
    time_str: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
    default_time_for_date: time | None = None,
) -> datetime:
    """Parse scheduler input into a future UTC-aware datetime."""
    return parse_future_datetime(
        time_str,
        now=now,
        timezone_name=timezone_name,
        default_time_for_date=default_time_for_date,
    )


def parse_time(time_str: str) -> datetime:
    """Compatibility wrapper for scheduler tools."""
    return parse_schedule_time(time_str)


def parse_date(
    date_str: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> str | None:
    """Parse a date string into ISO format (YYYY-MM-DD), or None on failure."""
    return parse_calendar_date(date_str, now=now, timezone_name=timezone_name)
