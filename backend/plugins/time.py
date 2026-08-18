"""
Time Utilities Plugin for JARV1S.
Handles time arithmetic that LLMs consistently hallucinate on:
duration calculations, countdowns, and world clock conversions.
"""

from datetime import datetime, time, timedelta, timezone
from typing import Dict

from zoneinfo import ZoneInfo, available_timezones

from pydantic import BaseModel
from core.decorators import tool
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.context import get_tz
from core.time import local_datetime_fields, parse_datetime

# Aliases for city names that don't match any IANA zone city part.
# Most cities (London, Tokyo, Sydney, etc.) are resolved by IANA lookup directly.
TIMEZONE_ALIASES: Dict[str, str] = {
    "nyc": "America/New_York",
    "la": "America/Los_Angeles",
    "sf": "America/Los_Angeles",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "bombay": "Asia/Kolkata",
    "beijing": "Asia/Shanghai",
    "peking": "Asia/Shanghai",
    "saigon": "Asia/Ho_Chi_Minh",
    "calcutta": "Asia/Kolkata",
    "uk": "Europe/London",
}


class TimeDelta(BaseModel):
    """Human-readable duration between two points in time."""
    days: int = 0
    hours: int = 0
    minutes: int = 0
    total_minutes: int = 0
    readable: str


class WorldTime(BaseModel):
    """Current time in a specific timezone."""
    timezone: str
    datetime: str
    readable: str
    offset: str


class TimePlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="time",
        version="1.0.0",
        description="Durations, countdowns, and world clock.",
        utterances=[
            "what time is it in Tokyo",
            "how long until Friday",
            "how many days until Christmas",
            "convert 3pm EST to my timezone",
            "what's the time difference with London",
        ],
    )

    @tool
    async def countdown(self, target: str) -> TimeDelta:
        """
        Calculate time remaining until a target time or date.
        Round to the most significant unit: "about 2 hours" not "1 hour 57 minutes 23 seconds".

        Args:
            target: Time ("17:00", "5pm"), date ("2026-12-25"), or datetime ("2026-03-15 09:00").
        """
        now = self._local_now()
        target_dt = self._parse_datetime(target, now)

        if target_dt <= now:
            return TimeDelta(readable="That time has already passed.", total_minutes=0)

        return self._build_delta(target_dt - now)

    @tool
    async def duration(self, start: str, end: str) -> TimeDelta:
        """
        Calculate duration between two times or dates.
        Use natural phrasing: "3 and a half hours" not "3 hours 30 minutes 0 seconds".

        Args:
            start: Time ("14:00", "2pm"), date ("2026-03-01"), or datetime.
            end: Time ("17:30", "5:30pm"), date ("2026-04-15"), or datetime.
        """
        now = self._local_now()
        start_dt = self._parse_datetime(start, now)
        end_dt = self._parse_datetime(end, now)

        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt

        return self._build_delta(end_dt - start_dt)

    @tool
    async def time_in(self, location: str) -> WorldTime:
        """
        Get the current time in another city or timezone.
        Use 12-hour format. Include the day if it differs from the user's local day.
        Do NOT read the timezone identifier aloud.

        Args:
            location: City name ("London", "Tokyo") or IANA timezone ("Europe/London").
        """
        tz = self._resolve_timezone(location)
        return self._world_time(tz, now_utc=datetime.now(timezone.utc), include_day_note=True)

    # --- Internal helpers (not exposed to LLM) ---

    def _local_now(self) -> datetime:
        return datetime.now(get_tz())

    def _world_time(
        self,
        tz: ZoneInfo,
        *,
        now_utc: datetime,
        include_day_note: bool,
    ) -> WorldTime:
        fields = local_datetime_fields(now_utc, timezone_name=str(tz))
        local_dt = datetime.fromisoformat(fields["time"])

        day_note = ""
        if include_day_note and local_dt.date() != now_utc.astimezone(get_tz()).date():
            day_note = f" ({local_dt.strftime('%A')})"

        utc_offset = local_dt.strftime("%z")
        offset_str = f"UTC{utc_offset[:3]}:{utc_offset[3:]}"

        return WorldTime(
            timezone=fields["timezone"],
            datetime=fields["time"],
            readable=f"{fields['local_time']}{day_note}",
            offset=offset_str,
        )

    def _resolve_timezone(self, location: str) -> ZoneInfo:
        key = location.lower().strip()

        # 1. Aliases for names that don't exist in IANA (mumbai, nyc, etc.)
        if key in TIMEZONE_ALIASES:
            return ZoneInfo(TIMEZONE_ALIASES[key])

        # 2. Exact IANA identifier ("America/New_York", "Europe/London")
        if location in available_timezones():
            return ZoneInfo(location)

        # 3. Match the city part of IANA zones (exact, not substring)
        for tz_name in sorted(available_timezones()):
            city = tz_name.split("/")[-1].lower().replace("_", " ")
            if key == city:
                return ZoneInfo(tz_name)

        raise ValueError(f"Unknown timezone or city: '{location}'")

    def _parse_datetime(self, value: str, reference: datetime) -> datetime:
        tz_name = getattr(reference.tzinfo, "key", None)
        return parse_datetime(
            value,
            now=reference,
            timezone_name=tz_name,
            default_time_for_date=time(0, 0),
        ).astimezone(reference.tzinfo)

    def _build_delta(self, delta: timedelta) -> TimeDelta:
        total_seconds = int(delta.total_seconds())
        total_minutes = total_seconds // 60
        days = delta.days
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60

        parts = []
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if not parts:
            parts.append("less than a minute")

        return TimeDelta(
            days=days,
            hours=hours,
            minutes=minutes,
            total_minutes=total_minutes,
            readable=", ".join(parts),
        )
