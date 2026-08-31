"""Shared parsing for user-facing time/date strings.

Schedulers should fail closed on missing intent; callers that truly accept
date-only input must opt in with an explicit default time.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from typing import TypedDict
from zoneinfo import ZoneInfo

from dateparser import DateDataParser  # type: ignore[import-not-found]

from core.context import ensure_aware, get_tz

_DURATION_UNIT_ALT = (
    r"seconds|secs|sec|minutes|mins|min|hours|hrs|hr|days|day"
    r"|(?<![a-z])(?:s|m|h|d)(?![a-z])"
)
_DURATION_PART_RE = re.compile(
    rf"(\d+(?:\.\d+)?)\s*(?P<unit>{_DURATION_UNIT_ALT})",
    re.IGNORECASE,
)
_TIME_ONLY_RE = re.compile(
    r"^(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$",
    re.IGNORECASE,
)
_OCLOCK_RE = re.compile(
    r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(?:o\s*['']?clock|oclock)\s*$",
    re.IGNORECASE,
)
_EXPLICIT_TIME_RE = re.compile(
    r"\b(?:\d{1,2}:\d{2}\s*(?:am|pm)?|\d{1,2}\s*(?:am|pm)|noon|midnight)\b",
    re.IGNORECASE,
)
_ISO_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WEEKDAY_EXPR_RE = re.compile(
    r"^(?:(next|this)\s+)?"
    r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+(?:at\s+)?(.+))?$",
    re.IGNORECASE,
)

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class LocalDateTimeFields(TypedDict):
    """User-facing local datetime fields plus UTC audit value."""

    time: str
    utc_time: str
    timezone: str
    local_time: str
    local_date: str


def coerce_timezone(tz_name: str | None) -> ZoneInfo:
    """Return a ZoneInfo, falling back to UTC for missing/invalid zone names."""
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _timezone_name(tz: ZoneInfo) -> str:
    return getattr(tz, "key", None) or "UTC"


def _has_explicit_time(value: str) -> bool:
    return bool(_EXPLICIT_TIME_RE.search(value))


def _strip_duration_prefix(value: str) -> str:
    raw = value.strip()
    if raw.lower().startswith("for "):
        raw = raw[4:].strip()
    if raw.lower().startswith("in "):
        raw = raw[3:].strip()
    if raw.startswith("+"):
        raw = raw[1:].strip()
    if raw.lower().endswith(" from now"):
        raw = raw[:-9].strip()
    return raw


def _duration_part_seconds(amount: float, unit: str) -> float:
    u = unit.lower()
    if u.startswith("s"):
        return amount
    if u.startswith("m"):
        return amount * 60
    if u.startswith("h"):
        return amount * 3600
    return amount * 86400


def _parse_duration_seconds(value: str) -> float | None:
    raw = _strip_duration_prefix(value)
    parts = list(_DURATION_PART_RE.finditer(raw))
    if not parts:
        return None

    last_end = 0
    total = 0.0
    for match in parts:
        if raw[last_end:match.start()].strip():
            return None
        total += _duration_part_seconds(float(match.group(1)), match.group("unit"))
        last_end = match.end()
    if raw[last_end:].strip():
        return None
    return total


def parse_duration(value: str, *, now: datetime | None = None) -> datetime | None:
    """Parse explicit durations like '5m', '5m 30s', or 'in 2 hours' into a UTC datetime."""
    total_seconds = _parse_duration_seconds(value)
    if total_seconds is None:
        return None
    if total_seconds <= 0:
        raise ValueError("Duration must be greater than zero")
    return _coerce_now(now) + timedelta(seconds=total_seconds)


def is_duration_expression(value: str) -> bool:
    """True when the string is a relative duration, not a wall-clock time."""
    return _parse_duration_seconds(value) is not None


def _normalize_clock_phrase(value: str) -> str:
    """Normalize spoken clock forms like ``5 o'clock`` into bare clock tokens."""
    raw = value.strip()
    match = _OCLOCK_RE.match(raw)
    if not match:
        return raw
    minute = match.group(2)
    hour = match.group(1)
    return f"{hour}:{minute}" if minute else hour


def normalize_clock_time(value: str | None) -> str | None:
    """Normalize user/display clock text to HH:MM for equality filters."""
    if value is None:
        return None
    normalized = _normalize_clock_phrase(value).strip()
    if not normalized:
        return None
    match = _TIME_ONLY_RE.match(normalized)
    if not match:
        return None

    hour_token = match.group(1)
    hour = int(hour_token)
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if minute > 59:
        return None
    try:
        hour_24 = _resolve_clock_hour(
            hour,
            minute,
            meridiem,
            has_colon=":" in normalized,
            zero_padded=_is_zero_padded_hour(hour_token),
        )
    except ValueError:
        return None
    return f"{hour_24:02d}:{minute:02d}"


def _is_zero_padded_hour(hour_token: str) -> bool:
    """Leading-zero HH (07) is 24-hour; bare 7 stays ambiguous 12-hour."""
    return len(hour_token) == 2 and hour_token.startswith("0")


def _resolve_clock_hour(
    hour: int,
    minute: int,
    meridiem: str,
    *,
    has_colon: bool,
    zero_padded: bool = False,
) -> int:
    if meridiem:
        if hour < 1 or hour > 12:
            raise ValueError(f"Invalid 12-hour time string")
        if meridiem == "am":
            return 0 if hour == 12 else hour
        return 12 if hour == 12 else hour + 12
    if hour > 23:
        raise ValueError(f"Invalid 24-hour time string")
    if has_colon and (hour > 12 or zero_padded):
        return hour
    if hour < 1 or hour > 12:
        raise ValueError(f"Invalid clock hour: {hour}")
    return hour


def _nearest_future_local_clock(
    hour: int,
    minute: int,
    meridiem: str,
    *,
    has_colon: bool,
    local_now: datetime,
    zero_padded: bool = False,
) -> datetime:
    if minute > 59:
        raise ValueError(f"Invalid minute in time string")

    if meridiem or (has_colon and (hour > 12 or zero_padded)):
        hour_24 = _resolve_clock_hour(
            hour,
            minute,
            meridiem,
            has_colon=has_colon,
            zero_padded=zero_padded,
        )
        target_local = local_now.replace(
            hour=hour_24, minute=minute, second=0, microsecond=0
        )
        if target_local <= local_now:
            target_local += timedelta(days=1)
        return target_local

    am_hour = 0 if hour == 12 else hour
    pm_hour = 12 if hour == 12 else hour + 12
    candidates: list[datetime] = []
    for hour_24 in (am_hour, pm_hour):
        target = local_now.replace(
            hour=hour_24, minute=minute, second=0, microsecond=0
        )
        if target <= local_now:
            target += timedelta(days=1)
        candidates.append(target)
    return min(candidates, key=lambda candidate: candidate - local_now)


def _parse_time_only(value: str, local_now: datetime) -> datetime | None:
    normalized = _normalize_clock_phrase(value)
    match = _TIME_ONLY_RE.match(normalized.strip())
    if not match:
        return None

    hour_token = match.group(1)
    hour = int(hour_token)
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    has_colon = ":" in normalized

    target_local = _nearest_future_local_clock(
        hour,
        minute,
        meridiem,
        has_colon=has_colon,
        local_now=local_now,
        zero_padded=_is_zero_padded_hour(hour_token),
    )
    return target_local.astimezone(timezone.utc)


def _parse_time_on_date(
    value: str,
    base_local: datetime,
    tz: ZoneInfo,
    *,
    local_now: datetime,
) -> datetime:
    normalized = _normalize_clock_phrase(value)
    match = _TIME_ONLY_RE.match(normalized.strip())
    if not match:
        raise ValueError(f"Could not parse time in weekday expression: {value!r}")

    hour_token = match.group(1)
    hour = int(hour_token)
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    has_colon = ":" in normalized

    if base_local.date() == local_now.date():
        anchor = local_now
    else:
        anchor = base_local.replace(hour=9, minute=0, second=0, microsecond=0)

    target_local = _nearest_future_local_clock(
        hour,
        minute,
        meridiem,
        has_colon=has_colon,
        local_now=anchor,
        zero_padded=_is_zero_padded_hour(hour_token),
    )
    return base_local.replace(
        hour=target_local.hour,
        minute=target_local.minute,
        second=0,
        microsecond=0,
    ).astimezone(tz).astimezone(timezone.utc)


def _weekday_date(prefix: str | None, weekday_name: str, local_now: datetime) -> datetime:
    target_wd = _WEEKDAYS[weekday_name.lower()]
    days_ahead = (target_wd - local_now.weekday()) % 7
    prefix = (prefix or "").lower()
    if prefix == "next":
        if days_ahead <= 1:
            days_ahead += 7
    elif prefix != "this" and days_ahead == 0:
        days_ahead = 7
    return (local_now + timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _parse_weekday_expression(
    value: str,
    *,
    local_now: datetime,
    tz: ZoneInfo,
    default_time_for_date: time | None,
) -> datetime | None:
    match = _WEEKDAY_EXPR_RE.match(value.strip())
    if not match:
        return None

    local_date = _weekday_date(match.group(1), match.group(2), local_now)
    time_text = match.group(3)
    if time_text:
        return _parse_time_on_date(time_text, local_date, tz, local_now=local_now)
    if default_time_for_date is None:
        raise ValueError(f"Missing time in schedule string: {value!r}")
    return local_date.replace(
        hour=default_time_for_date.hour,
        minute=default_time_for_date.minute,
    ).astimezone(timezone.utc)


def _parse_iso_date_only(
    value: str,
    *,
    tz: ZoneInfo,
    default_time_for_date: time | None,
) -> datetime | None:
    if not _ISO_DATE_ONLY_RE.match(value):
        return None
    if default_time_for_date is None:
        raise ValueError(f"Missing time in schedule string: {value!r}")
    parsed_date = datetime.fromisoformat(value).date()
    return datetime.combine(parsed_date, default_time_for_date, tzinfo=tz).astimezone(timezone.utc)


def _dateparser_settings(local_now: datetime, tz_name: str) -> dict:
    return {
        "RELATIVE_BASE": local_now,
        "TIMEZONE": tz_name,
        "TO_TIMEZONE": "UTC",
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "DATE_ORDER": "DMY",
        "PREFER_LOCALE_DATE_ORDER": False,
    }


def _parse_natural_datetime(
    value: str,
    *,
    local_now: datetime,
    tz: ZoneInfo,
    tz_name: str,
    default_time_for_date: time | None,
) -> datetime | None:
    parser = DateDataParser(languages=["en"], settings=_dateparser_settings(local_now, tz_name))
    data = parser.get_date_data(value.strip())
    parsed = data.date_obj
    if parsed is None:
        return None
    if data.period != "day":
        raise ValueError(f"Too broad to schedule precisely: {value!r}")

    parsed_utc = parsed.astimezone(timezone.utc)
    if not _has_explicit_time(value):
        if default_time_for_date is None:
            raise ValueError(f"Missing time in schedule string: {value!r}")
        parsed_local = parsed_utc.astimezone(tz)
        parsed_utc = parsed_local.replace(
            hour=default_time_for_date.hour,
            minute=default_time_for_date.minute,
            second=0,
            microsecond=0,
        ).astimezone(timezone.utc)
    return parsed_utc


def parse_datetime(
    value: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
    default_time_for_date: time | None = None,
) -> datetime:
    """Parse a user-facing datetime into UTC."""
    if not value or not value.strip():
        raise ValueError("Time string is required")

    utc_now = _coerce_now(now)
    tz = coerce_timezone(timezone_name) if timezone_name else get_tz()
    local_now = utc_now.astimezone(tz)
    raw = _normalize_clock_phrase(value.strip())

    parsed = parse_duration(raw, now=utc_now) or _parse_time_only(raw, local_now)
    if parsed is not None:
        return parsed

    parsed = _parse_weekday_expression(
        raw,
        local_now=local_now,
        tz=tz,
        default_time_for_date=default_time_for_date,
    )
    if parsed is not None:
        return parsed

    parsed = _parse_iso_date_only(raw, tz=tz, default_time_for_date=default_time_for_date)
    if parsed is not None:
        return parsed

    try:
        parsed = ensure_aware(raw, tz).astimezone(timezone.utc)
    except ValueError:
        parsed = _parse_natural_datetime(
            raw,
            local_now=local_now,
            tz=tz,
            tz_name=_timezone_name(tz),
            default_time_for_date=default_time_for_date,
        )

    if parsed is None:
        raise ValueError(f"Cannot parse time string: {value!r}")
    return parsed


def parse_future_datetime(
    value: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
    default_time_for_date: time | None = None,
) -> datetime:
    """Parse a user-facing future datetime into UTC.

    Date-only inputs are rejected unless default_time_for_date is provided.
    """
    utc_now = _coerce_now(now)
    parsed = parse_datetime(
        value,
        now=utc_now,
        timezone_name=timezone_name,
        default_time_for_date=default_time_for_date,
    )
    if parsed <= utc_now:
        raise ValueError(f"Parsed time is not in the future: {value!r}")
    return parsed


def parse_calendar_date(
    value: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> str | None:
    """Parse a user-facing date into YYYY-MM-DD."""
    value = value.strip()
    if _ISO_DATE_ONLY_RE.match(value):
        return value

    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        pass

    utc_now = _coerce_now(now)
    tz = coerce_timezone(timezone_name) if timezone_name else get_tz()
    local_now = utc_now.astimezone(tz)

    for fmt in ("%b %d", "%B %d"):
        try:
            parsed = datetime.strptime(value, fmt)
            candidate = parsed.replace(year=local_now.year)
            if candidate.date() < local_now.date():
                candidate = candidate.replace(year=local_now.year + 1)
            return candidate.date().isoformat()
        except ValueError:
            continue

    try:
        parsed = parse_future_datetime(
            value,
            now=utc_now,
            timezone_name=_timezone_name(tz),
            default_time_for_date=time(0, 0),
        )
    except ValueError:
        return None
    return parsed.astimezone(tz).date().isoformat()


def coerce_datetime_or_none(value: datetime | str | None) -> datetime | None:
    """Normalize persisted datetime-ish values to aware UTC datetimes, or None."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = ensure_aware(value, timezone.utc)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def coerce_datetime(value: datetime | str | None, *, default: datetime | None = None) -> datetime:
    """Normalize persisted datetime-ish values to aware UTC datetimes."""
    fallback = default or datetime.now(timezone.utc)
    return coerce_datetime_or_none(value) or fallback


def duration_minutes_between(start: datetime | str | None, end: datetime | str | None) -> int | None:
    """Return a positive whole-minute duration between two datetime-ish values."""
    start_dt = coerce_datetime_or_none(start)
    end_dt = coerce_datetime_or_none(end)
    if not start_dt or not end_dt:
        return None
    minutes = int((end_dt - start_dt).total_seconds() // 60)
    return minutes if minutes > 0 else None


def local_datetime_fields(
    value: datetime | str | None,
    *,
    timezone_name: str | None = None,
    default: datetime | None = None,
) -> LocalDateTimeFields:
    """Format a persisted UTC datetime for user-facing tool results."""
    utc_dt = coerce_datetime(value, default=default)
    tz = coerce_timezone(timezone_name) if timezone_name else get_tz()
    local_dt = utc_dt.astimezone(tz)
    return {
        "time": local_dt.isoformat(),
        "utc_time": utc_dt.isoformat(),
        "timezone": _timezone_name(tz),
        "local_time": local_dt.strftime("%I:%M %p").lstrip("0"),
        "local_date": local_dt.date().isoformat(),
    }


def build_turn_time_context(
    timezone_name: str | None = "UTC",
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Authoritative wall-clock fields injected into turn / background prompts."""
    utc_now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    if utc_now.tzinfo is None:
        utc_now = utc_now.replace(tzinfo=timezone.utc)
    else:
        utc_now = utc_now.astimezone(timezone.utc)
    fields = local_datetime_fields(utc_now, timezone_name=timezone_name or "UTC")
    local_now = datetime.fromisoformat(fields["time"])
    today = local_now.date()
    return {
        "local_time": local_now.strftime("%A, %Y-%m-%d %H:%M"),
        "local_time_iso": fields["time"],
        "local_time_clock": fields["local_time"],
        "utc_time": fields["utc_time"],
        "timezone": fields["timezone"],
        "today_date": today.isoformat(),
        "tomorrow_date": (today + timedelta(days=1)).isoformat(),
        "week_dates": ", ".join(
            f"{(today + timedelta(days=i)).strftime('%A')}={(today + timedelta(days=i)).isoformat()}"
            for i in range(7)
        ),
    }


def format_local_when(trigger_time: datetime, *, now: datetime | None = None) -> str:
    """Format a UTC trigger time for concise scheduler tool responses."""
    trigger_time = coerce_datetime(trigger_time, default=now)
    now_utc = now or datetime.now(timezone.utc)
    local_str = trigger_time.astimezone(get_tz()).strftime("%I:%M %p").lstrip("0")
    secs = max(0, int((trigger_time - now_utc).total_seconds()))
    if secs < 60:
        rel = f"{secs} seconds"
    else:
        mins, rem_secs = divmod(secs, 60)
        if mins < 60:
            rel = (
                f"{mins} minutes {rem_secs} seconds"
                if rem_secs
                else f"{mins} minutes"
            )
        else:
            hours, rem_mins = divmod(mins, 60)
            rel = (
                f"{hours} hours {rem_mins} minutes"
                if rem_mins
                else f"{hours} hours"
            )
    return f"{local_str} ({rel} from now)"
