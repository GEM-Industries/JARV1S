from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.scheduling import parse_date, parse_schedule_time
from core.time import duration_minutes_between, local_datetime_fields, parse_duration, parse_future_datetime
from core.time.parsing import format_local_when


NOW_UTC = datetime(2026, 5, 7, 6, 7, tzinfo=timezone.utc)
TZ_NAME = "Australia/Sydney"


def test_parse_duration_supports_single_and_compound_units() -> None:
    parsed = parse_duration("5m", now=NOW_UTC)
    assert parsed == NOW_UTC + timedelta(minutes=5)

    parsed = parse_duration("5m 30s", now=NOW_UTC)
    assert parsed == NOW_UTC + timedelta(minutes=5, seconds=30)

    parsed = parse_duration("in 1h 15m", now=NOW_UTC)
    assert parsed == NOW_UTC + timedelta(hours=1, minutes=15)

    parsed = parse_duration("5.5m", now=NOW_UTC)
    assert parsed == NOW_UTC + timedelta(minutes=5, seconds=30)

    parsed = parse_duration("30 minutes from now", now=NOW_UTC)
    assert parsed == NOW_UTC + timedelta(minutes=30)


def test_parse_duration_rejects_ambiguous_clock_times() -> None:
    assert parse_duration("5pm", now=NOW_UTC) is None


def test_parse_schedule_time_accepts_compound_timer_durations() -> None:
    parsed = parse_schedule_time("5m 30s", now=NOW_UTC, timezone_name=TZ_NAME)
    assert parsed == NOW_UTC + timedelta(minutes=5, seconds=30)


def test_parse_today_at_specific_time_in_local_timezone() -> None:
    parsed = parse_schedule_time("today 17:00", now=NOW_UTC, timezone_name=TZ_NAME)

    assert parsed == datetime(2026, 5, 7, 7, 0, tzinfo=timezone.utc)


def test_parse_specific_day_at_specific_time() -> None:
    parsed = parse_schedule_time("May 12 at 5pm", now=NOW_UTC, timezone_name=TZ_NAME)

    assert parsed == datetime(2026, 5, 12, 7, 0, tzinfo=timezone.utc)


def test_parse_next_weekday_at_specific_time() -> None:
    parsed = parse_schedule_time("next Tuesday at 09:30", now=NOW_UTC, timezone_name=TZ_NAME)

    assert parsed == datetime(2026, 5, 11, 23, 30, tzinfo=timezone.utc)


def test_parse_time_only_rolls_to_next_occurrence() -> None:
    parsed = parse_schedule_time("5pm", now=NOW_UTC, timezone_name=TZ_NAME)

    assert parsed == datetime(2026, 5, 7, 7, 0, tzinfo=timezone.utc)


def test_parse_date_only_is_rejected_without_default_time() -> None:
    with pytest.raises(ValueError, match="Missing time"):
        parse_schedule_time("tomorrow", now=NOW_UTC, timezone_name=TZ_NAME)


def test_parse_date_only_can_opt_into_default_time() -> None:
    parsed = parse_future_datetime(
        "tomorrow",
        now=NOW_UTC,
        timezone_name=TZ_NAME,
        default_time_for_date=datetime.min.time().replace(hour=9),
    )
    assert parsed == datetime(2026, 5, 7, 23, 0, tzinfo=timezone.utc)


def test_parse_rejects_past_explicit_today_time() -> None:
    with pytest.raises(ValueError, match="not in the future"):
        parse_schedule_time("today 15:00", now=NOW_UTC, timezone_name=TZ_NAME)


def test_parse_rejects_broad_periods() -> None:
    with pytest.raises(ValueError, match="Too broad"):
        parse_schedule_time("next week", now=NOW_UTC, timezone_name=TZ_NAME)


def test_parse_date_supports_prefixed_weekday() -> None:
    parsed = parse_date("next Friday", now=NOW_UTC, timezone_name=TZ_NAME)

    assert parsed == "2026-05-15"


def test_duration_minutes_between_normalizes_datetime_strings() -> None:
    assert duration_minutes_between(
        "2026-05-01T09:00:00+10:00",
        "2026-05-01T10:30:00+10:00",
    ) == 90


def test_duration_minutes_between_rejects_non_positive_or_invalid_values() -> None:
    assert duration_minutes_between(
        "2026-05-01T10:30:00+10:00",
        "2026-05-01T09:00:00+10:00",
    ) is None
    assert duration_minutes_between("not-a-date", "2026-05-01T09:00:00+10:00") is None


def test_local_datetime_fields_exposes_local_time_and_utc_audit_time() -> None:
    fields = local_datetime_fields(
        "2026-05-26T13:00:00Z",
        timezone_name="Australia/Sydney",
    )

    assert fields["time"] == "2026-05-26T23:00:00+10:00"
    assert fields["utc_time"] == "2026-05-26T13:00:00+00:00"
    assert fields["timezone"] == "Australia/Sydney"
    assert fields["local_time"] == "11:00 PM"
    assert fields["local_date"] == "2026-05-26"


@patch("core.time.parsing.get_tz", lambda: timezone.utc)
def test_format_local_when_shows_compound_minutes_and_seconds() -> None:
    now = datetime(2026, 5, 10, 9, 52, 0, tzinfo=timezone.utc)
    trigger = now + timedelta(minutes=5, seconds=30)
    s = format_local_when(trigger, now=now)
    assert "5 minutes 30 seconds from now" in s


@patch("core.time.parsing.get_tz", lambda: timezone.utc)
def test_format_local_when_shows_minutes_for_long_timers() -> None:
    now = datetime(2026, 5, 10, 9, 52, 0, tzinfo=timezone.utc)
    trigger = now + timedelta(minutes=3)
    s = format_local_when(trigger, now=now)
    assert "3 minutes from now" in s


@patch("core.time.parsing.get_tz", lambda: timezone.utc)
def test_format_local_when_shows_seconds_under_one_minute() -> None:
    now = datetime(2026, 5, 10, 9, 52, 0, tzinfo=timezone.utc)
    trigger = now + timedelta(seconds=45)
    s = format_local_when(trigger, now=now)
    assert "45 seconds from now" in s


def test_parse_nearest_future_ambiguous_clock_at_afternoon() -> None:
    # 3pm Sydney = 05:00 UTC
    now = datetime(2026, 5, 7, 5, 0, tzinfo=timezone.utc)
    parsed = parse_schedule_time("4:00", now=now, timezone_name=TZ_NAME)
    assert parsed == datetime(2026, 5, 7, 6, 0, tzinfo=timezone.utc)


def test_parse_nearest_future_ambiguous_clock_at_night() -> None:
    # 11pm Chicago = 2026-06-16 04:00 UTC
    now = datetime(2026, 6, 16, 4, 0, tzinfo=timezone.utc)
    parsed = parse_schedule_time("5:00", now=now, timezone_name="America/Chicago")
    assert parsed == datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)


def test_parse_zero_padded_clock_is_24h_not_nearest_pm() -> None:
    # Afternoon Sydney (17:15 local) — "07:30" must stay 07:30 next morning, not 19:30 tonight.
    now = datetime(2026, 7, 13, 7, 15, tzinfo=timezone.utc)
    parsed = parse_schedule_time("07:30", now=now, timezone_name=TZ_NAME)
    assert parsed == datetime(2026, 7, 13, 21, 30, tzinfo=timezone.utc)


def test_parse_unpadded_clock_still_picks_nearest_future() -> None:
    now = datetime(2026, 7, 13, 7, 15, tzinfo=timezone.utc)
    parsed = parse_schedule_time("7:30", now=now, timezone_name=TZ_NAME)
    assert parsed == datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc)


def test_parse_oclock_forms_as_clock_not_duration() -> None:
    now = datetime(2026, 6, 16, 4, 0, tzinfo=timezone.utc)
    parsed = parse_schedule_time("5 o'clock", now=now, timezone_name="America/Chicago")
    assert parsed == datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)


def test_normalize_clock_time_for_filters() -> None:
    from core.time import normalize_clock_time

    assert normalize_clock_time("9:30") == "09:30"
    assert normalize_clock_time("9:30 AM") == "09:30"
    assert normalize_clock_time("9:30 pm") == "21:30"
    assert normalize_clock_time("5 o'clock") == "05:00"
    assert normalize_clock_time("not a time") is None


def test_parse_twelve_oclock_is_clock_not_twelve_minutes() -> None:
    from core.time import is_duration_expression

    assert is_duration_expression("12 o'clock") is False
    now = datetime(2026, 5, 7, 6, 7, tzinfo=timezone.utc)
    parsed = parse_schedule_time("12 o'clock", now=now, timezone_name=TZ_NAME)
    assert parsed > now


def test_parse_bare_hour_without_meridiem() -> None:
    now = datetime(2026, 5, 7, 5, 0, tzinfo=timezone.utc)
    parsed = parse_schedule_time("4", now=now, timezone_name=TZ_NAME)
    assert parsed == datetime(2026, 5, 7, 6, 0, tzinfo=timezone.utc)
