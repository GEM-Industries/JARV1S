"""
Calendar return-type models.

Lives in its own module so provider implementations can import CalendarEvent /
EventConfirmation without circularly importing the plugin barrel.
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from core.plugins.read_evidence import MatchStatus, ReadCoverage

CalendarRecurrence = Literal["daily", "weekdays", "weekly", "monthly", "yearly"]


class CalendarEvent(BaseModel):
    id: str
    title: str
    start: str
    end: str
    location: Optional[str] = None
    description: Optional[str] = None
    is_all_day: bool = False
    attendees: List[str] = Field(default_factory=list)
    attendee_count: int = 0
    duration_minutes: Optional[int] = None
    meet_link: Optional[str] = None
    account: Optional[str] = None  # "personal" | "work" — origin label for write routing
    scope: Literal["event", "occurrence", "series"] = "event"
    series_id: Optional[str] = None
    recurrence: Optional[CalendarRecurrence] = None

    def __str__(self) -> str:
        parts = [self.title, self.start]
        if self.duration_minutes:
            parts.append(f"{self.duration_minutes}m")
        if self.location and not self.location.startswith("http"):
            parts.append(self.location)
        if self.meet_link:
            parts.append("Meet")
        elif self.location and "zoom" in self.location.lower():
            parts.append("Zoom")
        if self.attendee_count:
            parts.append(f"{self.attendee_count} attendees")
        if self.is_all_day:
            parts.append("all-day")
        if self.recurrence:
            parts.append(self.recurrence)
        if self.account:
            parts.append(f"[{self.account}]")
        # Last 12 chars of provider IDs contain the instance timestamp
        # (e.g. 20260413T060000Z for Google) — unique within a query result set.
        parts.append(f"id:{self.id[-12:]}")
        return " | ".join(parts)


class EventConfirmation(BaseModel):
    id: str
    title: str
    start: str
    end: str
    html_link: Optional[str] = None
    meet_link: Optional[str] = None
    conflicts: List[CalendarEvent] = Field(default_factory=list)
    account: Optional[str] = None
    scope: Literal["event", "occurrence", "series"] = "event"
    series_id: Optional[str] = None
    recurrence: Optional[CalendarRecurrence] = None


class CalendarQueryResult(BaseModel):
    """Events plus enough coverage evidence to interpret an empty result."""

    events: List[CalendarEvent] = Field(default_factory=list)
    time_min: str
    time_max: str
    query: Optional[str] = None
    match_status: MatchStatus
    coverage: ReadCoverage
    truncated: bool = False
    failed_providers: List[str] = Field(default_factory=list)


class TimeSlot(BaseModel):
    start: str
    end: str
    duration_minutes: int
