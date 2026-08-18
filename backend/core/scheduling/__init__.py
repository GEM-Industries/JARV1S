"""Scheduling primitives shared by the scheduler service and plugin.

Single source of truth for:
  * recurrence validation + description + next-occurrence math
  * time/date string parsing used by scheduler tools
"""

from core.scheduling.recurrence import (
    VALID_RECURRENCE_PRESETS,
    describe,
    is_valid,
    next_occurrence,
    recurrence_rule_from_origin,
)
from core.scheduling.time_parsing import (
    coerce_datetime,
    coerce_datetime_or_none,
    coerce_timezone,
    format_local_when,
    LocalDateTimeFields,
    local_datetime_fields,
    parse_date,
    parse_schedule_time,
    parse_time,
)

__all__ = [
    "VALID_RECURRENCE_PRESETS",
    "coerce_datetime",
    "coerce_datetime_or_none",
    "coerce_timezone",
    "describe",
    "format_local_when",
    "is_valid",
    "LocalDateTimeFields",
    "local_datetime_fields",
    "next_occurrence",
    "parse_date",
    "parse_schedule_time",
    "parse_time",
    "recurrence_rule_from_origin",
]
