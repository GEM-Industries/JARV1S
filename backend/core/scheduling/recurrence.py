"""
Recurrence rule parsing and next-occurrence computation for scheduled triggers.

Owns the single source of truth for what a valid recurrence string looks like
and how it advances. Used by both the trigger scheduler and scheduler plugin.

Catch-up safe: `next_occurrence` always computes from the passed-in `now`,
not from a previous trigger_time, so a server outage won't cause a burst of
firings when the process comes back up.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.scheduling.time_parsing import coerce_datetime_or_none, coerce_timezone

logger = logging.getLogger(__name__)

VALID_RECURRENCE_PRESETS = {"daily", "weekdays", "weekends", "weekly"}
RECURRENCE_ALIASES = {
    "every day": "daily",
    "everyday": "daily",
    "each day": "daily",
    "every weekday": "weekdays",
    "every week day": "weekdays",
    "week days": "weekdays",
    "every weekend": "weekends",
    "every week": "weekly",
}

_INTERVAL_RE = re.compile(r"every\s*(\d+)\s*(s|m|h|sec|min|hour)", re.IGNORECASE)
_INTERVAL_RE_FULL = re.compile(
    r"^every\s*\d+\s*(s|m|h|sec|min|hour)\w*$", re.IGNORECASE
)


def normalize_recurrence(recurrence: str) -> str:
    """Normalize common user-facing recurrence synonyms."""
    r = " ".join(recurrence.lower().strip().split())
    return RECURRENCE_ALIASES.get(r, r)


def is_valid(recurrence: str) -> bool:
    """True if `recurrence` is a supported preset or `every N<unit>` interval."""
    r = normalize_recurrence(recurrence)
    if r in VALID_RECURRENCE_PRESETS:
        return True
    return bool(_INTERVAL_RE_FULL.match(r))


def describe(alert: dict) -> str:
    """Human-readable description of a scheduled trigger's recurrence."""
    rec = normalize_recurrence(alert.get("recurrence", ""))
    local_time = alert.get("original_local_time", "")

    time_str = ""
    if local_time:
        h, m = map(int, local_time.split(":"))
        period = "AM" if h < 12 else "PM"
        display_h = h % 12 or 12
        time_str = f" at {display_h}:{m:02d} {period}"

    if rec == "daily":
        return f"Daily{time_str}"
    if rec == "weekdays":
        return f"Weekdays{time_str}"
    if rec == "weekends":
        return f"Weekends{time_str}"
    if rec == "weekly":
        trigger = alert.get("trigger_time")
        if trigger and hasattr(trigger, "strftime"):
            tz_name = alert.get("timezone", "UTC")
            day = trigger.astimezone(coerce_timezone(tz_name)).strftime("%A")
            return f"Every {day}{time_str}"
        return f"Weekly{time_str}"
    if rec.startswith("every"):
        return rec.capitalize()
    return rec


def recurrence_rule_from_origin(
    origin: dict[str, Any],
    *,
    rule_doc: dict[str, Any] | None = None,
    owner_id: str | None = None,
    rule_id: str | None = None,
) -> dict[str, Any]:
    """Build the normalized input expected by next_occurrence from trigger data."""
    rule_doc = rule_doc or {}
    tz_name = origin.get("timezone") or "UTC"
    tz = coerce_timezone(tz_name)
    fire_at = coerce_datetime_or_none(origin.get("fire_at"))

    original_local_time = origin.get("original_local_time")
    if not original_local_time and fire_at:
        original_local_time = fire_at.astimezone(tz).strftime("%H:%M")

    recurrence_rule: dict[str, Any] = {
        "recurrence": origin.get("recurrence", ""),
        "timezone": tz_name,
        "exceptions": rule_doc.get("exceptions", []),
    }
    if original_local_time:
        recurrence_rule["original_local_time"] = original_local_time
    if fire_at:
        recurrence_rule["trigger_time"] = fire_at
        recurrence_rule["original_weekday"] = fire_at.astimezone(tz).weekday()
    if owner_id:
        recurrence_rule["owner_id"] = owner_id
    if rule_id:
        recurrence_rule["series_id"] = rule_id
    return recurrence_rule


def next_occurrence(rule: dict, now: datetime) -> Optional[datetime]:
    """
    Compute the next FUTURE trigger time for a recurring trigger rule.

    Catch-up safe: always computes from 'now', not from the previous
    trigger_time, so a server outage won't cause a burst of firings.
    """
    recurrence = normalize_recurrence(rule.get("recurrence", ""))

    # --- Interval-based: "every 2h", "every 30m" ---
    match = _INTERVAL_RE.match(recurrence)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("h"):
            return now + timedelta(hours=amount)
        if unit.startswith("m"):
            return now + timedelta(minutes=amount)
        return now + timedelta(seconds=amount)

    # --- Day-based: daily, weekdays, weekends, weekly ---
    tz_name = rule.get("timezone", "UTC")
    tz = coerce_timezone(tz_name)
    trigger_time = coerce_datetime_or_none(rule.get("trigger_time"))

    local_time_str = rule.get("original_local_time")
    if not local_time_str and trigger_time:
        local_time_str = trigger_time.astimezone(tz).strftime("%H:%M")
    if not local_time_str:
        local_time_str = "08:00"

    try:
        hour, minute = map(int, local_time_str.split(":"))
    except (AttributeError, TypeError, ValueError):
        logger.warning("Invalid original_local_time for recurrence: %r", local_time_str)
        return None
    local_now = now.astimezone(tz)

    # DST guard: if the target local time doesn't exist (spring-forward gap),
    # shift to the first valid time after the gap.
    try:
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (ValueError, OverflowError):
        candidate = local_now.replace(hour=hour + 1, minute=0, second=0, microsecond=0)

    if candidate <= local_now:
        candidate += timedelta(days=1)

    if recurrence == "weekdays":
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    elif recurrence == "weekends":
        while candidate.weekday() < 5:
            candidate += timedelta(days=1)
    elif recurrence == "weekly":
        original_weekday = rule.get("original_weekday")
        if original_weekday is None and trigger_time:
            original_weekday = trigger_time.astimezone(tz).weekday()
        if original_weekday is not None:
            while candidate.weekday() != original_weekday:
                candidate += timedelta(days=1)
    elif recurrence != "daily":
        logger.warning(f"Unknown recurrence pattern: {recurrence}")
        return None

    exceptions = set(rule.get("exceptions", []))
    if exceptions:
        for _ in range(366):
            if candidate.date().isoformat() not in exceptions:
                break
            candidate += timedelta(days=1)

    return candidate.astimezone(timezone.utc)
