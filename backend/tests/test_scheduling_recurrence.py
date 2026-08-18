from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.scheduling import is_valid, next_occurrence, recurrence_rule_from_origin


SYDNEY = ZoneInfo("Australia/Sydney")


def test_recurrence_rule_from_origin_preserves_original_local_time():
    origin = {
        "kind": "time",
        "recurrence": "daily",
        "timezone": "Australia/Sydney",
        "fire_at": "2026-05-04T08:30:00+00:00",
        "original_local_time": "18:30",
    }

    rule = recurrence_rule_from_origin(origin, rule_doc={"exceptions": []})
    next_time = next_occurrence(rule, datetime(2026, 5, 4, 23, 0, tzinfo=timezone.utc))

    assert next_time is not None
    local = next_time.astimezone(SYDNEY)
    assert (local.hour, local.minute) == (18, 30)


def test_recurrence_accepts_common_synonyms():
    assert is_valid("every day")
    assert is_valid("every weekday")

    next_time = next_occurrence(
        {
            "recurrence": "every day",
            "timezone": "Australia/Sydney",
            "original_local_time": "18:30",
        },
        datetime(2026, 5, 4, 23, 0, tzinfo=timezone.utc),
    )
    assert next_time is not None
    assert next_time.astimezone(SYDNEY).strftime("%H:%M") == "18:30"


def test_recurrence_rule_from_origin_derives_local_time_from_fire_at():
    origin = {
        "kind": "time",
        "recurrence": "daily",
        "timezone": "Australia/Sydney",
        "fire_at": "2026-05-04T08:30:00+00:00",
    }

    rule = recurrence_rule_from_origin(origin)
    next_time = next_occurrence(rule, datetime(2026, 5, 4, 23, 0, tzinfo=timezone.utc))

    assert next_time is not None
    local = next_time.astimezone(SYDNEY)
    assert (local.hour, local.minute) == (18, 30)


def test_weekly_recurrence_keeps_original_weekday():
    origin = {
        "kind": "time",
        "recurrence": "weekly",
        "timezone": "Australia/Sydney",
        "fire_at": "2026-05-04T08:30:00+00:00",
        "original_local_time": "18:30",
    }

    rule = recurrence_rule_from_origin(origin)
    next_time = next_occurrence(rule, datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc))

    assert next_time is not None
    local = next_time.astimezone(SYDNEY)
    assert local.strftime("%A") == "Monday"
    assert (local.hour, local.minute) == (18, 30)


def test_day_based_recurrence_respects_exceptions_at_original_local_time():
    origin = {
        "kind": "time",
        "recurrence": "daily",
        "timezone": "Australia/Sydney",
        "fire_at": "2026-05-04T08:30:00+00:00",
        "original_local_time": "18:30",
    }

    rule = recurrence_rule_from_origin(
        origin,
        rule_doc={"exceptions": ["2026-05-04"]},
    )
    next_time = next_occurrence(rule, datetime(2026, 5, 4, 0, 0, tzinfo=timezone.utc))

    assert next_time is not None
    local = next_time.astimezone(SYDNEY)
    assert local.date().isoformat() == "2026-05-05"
    assert (local.hour, local.minute) == (18, 30)
