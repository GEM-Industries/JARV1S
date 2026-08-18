"""Pure scheduled-attention resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from core.attention.models import AttentionMode, AttentionSource, ManualOverride, QuietWindow
from core.scheduling.time_parsing import coerce_timezone

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True)
class ScheduledAttentionResolution:
    """Desired scheduled attention for one owner at one instant."""

    mode: Literal["quiet"] | None
    active_schedule_ids: tuple[str, ...]
    effective_until: datetime | None


@dataclass(frozen=True, slots=True)
class EffectiveAttention:
    """The derived attention for one owner at one instant."""

    mode: AttentionMode
    source: AttentionSource
    expires_at: datetime | None
    active_window_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Window:
    schedule_id: str
    start: datetime
    end: datetime


def _parse_hhmm(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour=hour, minute=minute)


def _localize_time(local_date: date, local_time: time, tz: ZoneInfo) -> datetime:
    """Build an aware local datetime, shifting forward on spring-forward gaps."""
    candidate = datetime.combine(local_date, local_time, tzinfo=tz)
    round_tripped = candidate.astimezone(timezone.utc).astimezone(tz)
    if round_tripped.replace(tzinfo=None) == candidate.replace(tzinfo=None):
        return candidate

    # Nonexistent wall times during spring-forward resolve to the first valid
    # minute after the gap. Ambiguous fall-back times keep ZoneInfo's fold=0.
    shifted = candidate
    for _ in range(120):
        shifted += timedelta(minutes=1)
        round_tripped = shifted.astimezone(timezone.utc).astimezone(tz)
        if round_tripped.replace(tzinfo=None) == shifted.replace(tzinfo=None):
            return shifted
    return candidate


def _schedule_windows(schedule: QuietWindow, now_utc: datetime) -> list[_Window]:
    if not schedule.enabled:
        return []

    tz = coerce_timezone(schedule.timezone)
    local_now = now_utc.astimezone(tz)
    start = _parse_hhmm(schedule.start_time)
    end = _parse_hhmm(schedule.end_time)
    allowed_days = {day.lower() for day in schedule.days}

    if start == end:
        return []

    windows: list[_Window] = []
    for offset in range(-1, 3):
        start_date = local_now.date() + timedelta(days=offset)
        weekday = WEEKDAY_NAMES[start_date.weekday()]
        if weekday not in allowed_days:
            continue

        start_dt = _localize_time(start_date, start, tz)
        end_date = start_date if start < end else start_date + timedelta(days=1)
        end_dt = _localize_time(end_date, end, tz)
        windows.append(
            _Window(
                schedule_id=schedule.id,
                start=start_dt.astimezone(timezone.utc),
                end=end_dt.astimezone(timezone.utc),
            )
        )
    return windows


def resolve_scheduled_attention(
    now_utc: datetime,
    schedules: list[QuietWindow],
) -> ScheduledAttentionResolution:
    """Return the desired scheduled attention mode for enabled schedules."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    windows: list[_Window] = []
    for schedule in schedules:
        windows.extend(_schedule_windows(schedule, now_utc))

    active = [window for window in windows if window.start <= now_utc < window.end]
    if not active:
        return ScheduledAttentionResolution(mode=None, active_schedule_ids=(), effective_until=None)

    window_start = min(window.start for window in active)
    window_end = max(window.end for window in active)
    active_ids = {window.schedule_id for window in active}

    changed = True
    while changed:
        changed = False
        for window in windows:
            if window.end < window_start or window.start > window_end:
                continue
            active_ids.add(window.schedule_id)
            if window.start < window_start:
                window_start = window.start
                changed = True
            if window.end > window_end:
                window_end = window.end
                changed = True

    return ScheduledAttentionResolution(
        mode="quiet",
        active_schedule_ids=tuple(sorted(active_ids)),
        effective_until=window_end,
    )


def resolve_effective_attention(
    now_utc: datetime,
    override: ManualOverride | None,
    windows: list[QuietWindow],
) -> EffectiveAttention:
    """Derive the effective attention from the manual override and quiet windows.

    A live manual override always wins while it lasts; otherwise an active quiet
    window applies; otherwise the owner is active. This is the single source of
    truth for "what mode are we in" — nothing else stores the answer.
    """
    if override is not None and (override.expires_at is None or override.expires_at > now_utc):
        return EffectiveAttention(
            mode=override.mode,
            source=override.source,
            expires_at=override.expires_at,
            active_window_ids=(),
        )

    scheduled = resolve_scheduled_attention(now_utc, windows)
    if scheduled.mode == "quiet":
        return EffectiveAttention(
            mode="quiet",
            source="schedule",
            expires_at=scheduled.effective_until,
            active_window_ids=scheduled.active_schedule_ids,
        )

    return EffectiveAttention(
        mode="active",
        source="default",
        expires_at=None,
        active_window_ids=(),
    )
