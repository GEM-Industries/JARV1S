"""EventKit calendar backend via the Host loopback API."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import httpx

from core.integrations.manager import OsPermissionNeeded
from core.plugins.capabilities import CapabilityErrorDetail
from core.time import coerce_datetime_or_none, duration_minutes_between
from plugins.calendar.models import CalendarEvent, EventConfirmation
from plugins.calendar.providers.base import ProviderEventBatch

logger = logging.getLogger(__name__)

CALENDAR_ACCESS_DENIED = (
    "Calendar access is turned off on this Mac. "
    "Enable it in System Settings → Privacy & Security → Calendars."
)
CALENDAR_ACCESS_REQUIRED = "Calendar access required on this Mac"
CALENDAR_ACCESS_GRANTED = "Calendar access is on for this Mac."
CALENDAR_ACCESS_PROMPT = (
    "Allow Calendar access when prompted, or enable it in "
    "System Settings → Privacy & Security → Calendars."
)

_DENIED_STATUSES = frozenset({"denied", "restricted"})
_UNSUPPORTED = CapabilityErrorDetail(
    code="unsupported",
    message=(
        "Calendar on this Mac is read-only. Connect Google or Microsoft to add or change events."
    ),
)
_EVENTS_TIMEOUT = 30.0
_AUTHORIZE_TIMEOUT = 125.0


def macos_calendar_denied(status: str) -> bool:
    return status in _DENIED_STATUSES


def macos_calendar_message(status: str) -> str:
    if status == "authorized":
        return CALENDAR_ACCESS_GRANTED
    if macos_calendar_denied(status):
        return CALENDAR_ACCESS_DENIED
    return CALENDAR_ACCESS_PROMPT


def _host_config() -> tuple[str, str] | None:
    from core.config import settings

    url = (settings.HOST_CALENDAR_URL or "").strip()
    if not url:
        return None
    return url.rstrip("/"), settings.HOST_CALENDAR_TOKEN or ""


def host_calendar_configured() -> bool:
    return _host_config() is not None


async def macos_connection_state() -> tuple[bool, str | None]:
    """Return (host available, EventKit status or None if the host is missing)."""
    if not host_calendar_configured():
        return False, None
    try:
        return True, await macos_calendar_status()
    except Exception:
        logger.warning("Host calendar status check failed", exc_info=True)
        return True, None


def _window_for_event_id(event_id: str) -> tuple[str, str]:
    """Day encoded in the Host id, plus one day either side for timezone edges."""
    start = event_id.rsplit("|", 1)[-1] if "|" in event_id else event_id
    instant = coerce_datetime_or_none(start)
    if instant is None:
        raise RuntimeError(f"Event {event_id!r} is not a Mac calendar id")
    lo = instant - timedelta(days=1)
    hi = instant + timedelta(days=1)
    return lo.isoformat(), hi.isoformat()


def _event_from_host(item: dict[str, Any]) -> CalendarEvent:
    attendees = [str(value) for value in (item.get("attendees") or []) if value]
    recurrence = item.get("recurrence")
    if recurrence not in {"daily", "weekdays", "weekly", "monthly", "yearly"}:
        recurrence = None
    is_all_day = bool(item.get("is_all_day"))
    start = str(item.get("start") or "")
    end = str(item.get("end") or "")
    return CalendarEvent(
        id=str(item.get("id") or ""),
        title=item.get("title") or "(No title)",
        start=start,
        end=end,
        location=item.get("location") or None,
        description=item.get("description") or None,
        is_all_day=is_all_day,
        attendees=attendees,
        attendee_count=len(attendees),
        calendar=item.get("calendar") or None,
        account="macos",
        recurrence=recurrence,
        duration_minutes=(
            None if is_all_day else duration_minutes_between(start, end)
        ),
    )


def _payload_or_error(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code == 403:
        raise OsPermissionNeeded(CALENDAR_ACCESS_DENIED)
    if resp.status_code == 401:
        raise RuntimeError("Host calendar authentication failed")
    if resp.status_code in {503, 504}:
        raise RuntimeError("Calendar on this Mac is not responding. Try again.")
    resp.raise_for_status()
    payload = resp.json()
    return payload if isinstance(payload, dict) else {}


async def _host_request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    timeout: float = _EVENTS_TIMEOUT,
) -> dict[str, Any]:
    config = _host_config()
    if config is None:
        raise RuntimeError("Calendar on this Mac is not available.")
    url, token = config
    try:
        async with httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        ) as client:
            resp = await client.request(method, path, params=params)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Host calendar unreachable: {exc}") from exc
    return _payload_or_error(resp)


async def macos_calendar_status() -> str:
    data = await _host_request("GET", "/status", timeout=5.0)
    return str(data.get("status") or "notDetermined")


async def authorize_macos_calendar() -> str:
    data = await _host_request("POST", "/authorize", timeout=_AUTHORIZE_TIMEOUT)
    return str(data.get("status") or "notDetermined")


class EventKitProvider:
    """CalendarProvider backed by the Host EventKit loopback."""

    name = "macos"

    async def list_events(
        self,
        time_min: str,
        time_max: str,
        max_results: int = 50,
    ) -> ProviderEventBatch:
        events = await self._events(time_min, time_max)
        events.sort(key=lambda event: event.start)
        return ProviderEventBatch(events=events[:max_results], incomplete=False)

    async def get_event(self, event_id: str) -> CalendarEvent:
        time_min, time_max = _window_for_event_id(event_id)
        events = await self._events(time_min, time_max, event_id=event_id)
        if not events:
            raise RuntimeError(f"Event {event_id!r} not found on this Mac")
        return events[0]

    async def search_events(
        self,
        query: str,
        time_min: str,
        time_max: str,
        max_results: int = 20,
    ) -> ProviderEventBatch:
        from plugins.calendar.unified import _matches_query

        events = await self._events(time_min, time_max)
        matched = [event for event in events if _matches_query(event, query)]
        matched.sort(key=lambda event: event.start)
        return ProviderEventBatch(events=matched[:max_results], incomplete=False)

    async def create_event(self, *args: Any, **kwargs: Any) -> EventConfirmation | CapabilityErrorDetail:
        return _UNSUPPORTED

    async def update_event(self, *args: Any, **kwargs: Any) -> EventConfirmation | CapabilityErrorDetail:
        return _UNSUPPORTED

    async def delete_event(self, event_id: str) -> str | CapabilityErrorDetail:
        return _UNSUPPORTED

    async def refresh(self) -> None:
        return None

    async def _events(
        self,
        time_min: str,
        time_max: str,
        event_id: str | None = None,
    ) -> list[CalendarEvent]:
        params: dict[str, str] = {"time_min": time_min, "time_max": time_max}
        if event_id:
            params["id"] = event_id
        data = await _host_request("GET", "/events", params=params)
        return [_event_from_host(item) for item in data.get("events") or [] if item.get("id")]


def try_eventkit_provider() -> EventKitProvider | None:
    if not host_calendar_configured():
        return None
    return EventKitProvider()
