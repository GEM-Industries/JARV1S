"""
CalendarProvider Protocol.

Each backend (Google, Outlook, EventKit, …) implements this interface. The
UnifiedCalendarClient fans out across providers for reads and routes writes
to a single provider by connection name (`google` | `microsoft` | `macos`).

Providers return already-parsed CalendarEvent / EventConfirmation — raw dicts do not cross
the protocol boundary. Google-specific and Graph-specific shapes are reconciled inside
each provider implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, runtime_checkable

import httpx

from core.plugins.capabilities import CapabilityErrorDetail
from plugins.calendar.models import CalendarEvent, CalendarRecurrence, EventConfirmation


@dataclass(frozen=True)
class ProviderEventBatch:
    events: List[CalendarEvent]
    incomplete: bool = False


class _ProviderBase:
    """Shared httpx transport and account-stamping for Google and Outlook."""

    name: str = ""  # overridden by subclasses

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        """Transport client — exposed for the push adapter's watch registration."""
        return self._client

    def _stamp(self, event: CalendarEvent, calendar: Optional[str] = None) -> CalendarEvent:
        update = {"account": self.name}
        if calendar:
            update["calendar"] = calendar
        return event.model_copy(update=update)


@runtime_checkable
class CalendarProvider(Protocol):
    """Minimal async interface every calendar backend must satisfy."""

    name: str  # "google" | "microsoft" | "macos"

    async def list_events(
        self,
        time_min: str,
        time_max: str,
        max_results: int = 50,
    ) -> ProviderEventBatch:
        """Return events in the time window across every user-visible calendar on this provider."""
        ...

    async def get_event(self, event_id: str) -> CalendarEvent:
        """Fetch full details for a single event. Raises on not found."""
        ...

    async def create_event(
        self,
        title: str,
        start: str,
        duration_minutes: int = 30,
        end: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[List[str]] = None,
        add_meet: bool = False,
        all_day: bool = False,
        recurrence: Optional[CalendarRecurrence] = None,
        tz_name: Optional[str] = None,
    ) -> EventConfirmation | CapabilityErrorDetail:
        """Create an event on this provider's primary calendar."""
        ...

    async def update_event(
        self,
        event_id: str,
        title: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        duration_minutes: Optional[int] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[List[str]] = None,
        add_meet: bool = False,
        recurrence: Optional[CalendarRecurrence] = None,
        tz_name: Optional[str] = None,
    ) -> EventConfirmation | CapabilityErrorDetail:
        """Patch an existing event. Only supplied fields change."""
        ...

    async def search_events(
        self,
        query: str,
        time_min: str,
        time_max: str,
        max_results: int = 20,
    ) -> ProviderEventBatch:
        """Full-text search across events in the time window."""
        ...

    async def delete_event(self, event_id: str) -> str | CapabilityErrorDetail:
        """Delete an event by id. Returns confirmation text or a typed not-found error."""
        ...

    async def refresh(self) -> None:
        """Refresh OAuth credentials for this provider."""
        ...
