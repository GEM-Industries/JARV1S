"""
GoogleProvider — Google Calendar backend behind the UnifiedCalendarClient.

Owns its own httpx.AsyncClient, calendar-ID cache, and Google-specific event
parsing. Stamps `account` on every returned CalendarEvent so the LLM can
route subsequent mutations through the same account label.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote as _urlquote
from uuid import uuid4

import httpx

from core.auth.manager import auth_manager
from core.integrations.manager import NeedsReauth
from core.time import duration_minutes_between

from core.plugins.capabilities import CapabilityErrorDetail

from plugins.calendar.models import CalendarEvent, CalendarRecurrence, EventConfirmation
from plugins.calendar.providers.base import ProviderEventBatch, _ProviderBase

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"

GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]

_SINGLE_EVENT_FIELDS = (
    "id,summary,start,end,location,description,status,recurringEventId,recurrence,"
    "attendees(email,displayName,self,responseStatus),"
    "hangoutLink,conferenceData(entryPoints(entryPointType,uri))"
)
_EVENT_FIELDS = f"items({_SINGLE_EVENT_FIELDS})"

_WEEKDAY_BY_INDEX = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _anchor_datetime(start: str) -> datetime:
    if len(start) == 10:
        return datetime.fromisoformat(f"{start}T00:00:00")
    return datetime.fromisoformat(start)


def _google_rrule(recurrence: CalendarRecurrence, start: str) -> str:
    anchor = _anchor_datetime(start)
    if recurrence == "daily":
        return "RRULE:FREQ=DAILY"
    if recurrence == "weekdays":
        return "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    if recurrence == "weekly":
        return f"RRULE:FREQ=WEEKLY;BYDAY={_WEEKDAY_BY_INDEX[anchor.weekday()]}"
    if recurrence == "monthly":
        return f"RRULE:FREQ=MONTHLY;BYMONTHDAY={anchor.day}"
    if recurrence == "yearly":
        return f"RRULE:FREQ=YEARLY;BYMONTH={anchor.month};BYMONTHDAY={anchor.day}"
    raise ValueError(f"Unsupported recurrence={recurrence!r}")


def _normalize_google_recurrence(raw: Any) -> Optional[CalendarRecurrence]:
    if not isinstance(raw, list):
        return None
    joined = " ".join(str(item) for item in raw).upper()
    if "FREQ=YEARLY" in joined:
        return "yearly"
    if "FREQ=MONTHLY" in joined:
        return "monthly"
    if "FREQ=WEEKLY" in joined and "BYDAY=MO,TU,WE,TH,FR" in joined.replace(" ", ""):
        return "weekdays"
    if "FREQ=WEEKLY" in joined:
        return "weekly"
    if "FREQ=DAILY" in joined:
        return "daily"
    return None


def _confirmation_from_google_item(
    item: Dict[str, Any],
    *,
    account: Optional[str],
    fallback_title: str = "",
    fallback_start: str = "",
    fallback_end: str = "",
) -> EventConfirmation:
    parsed = _parse_event(item)
    return EventConfirmation(
        id=item["id"],
        title=item.get("summary", fallback_title) or parsed.title,
        start=(
            item.get("start", {}).get("dateTime")
            or item.get("start", {}).get("date", fallback_start or parsed.start)
        ),
        end=(
            item.get("end", {}).get("dateTime")
            or item.get("end", {}).get("date", fallback_end or parsed.end)
        ),
        html_link=item.get("htmlLink"),
        meet_link=_extract_meet_link(item),
        account=account,
        scope=parsed.scope,
        series_id=parsed.series_id or (item["id"] if parsed.scope == "series" else None),
        recurrence=parsed.recurrence,
    )


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return f"HTTP {response.status_code} {response.reason_phrase}"
    if isinstance(exc, httpx.RequestError):
        return f"{type(exc).__name__}: {exc.request.method} {exc.request.url.path}"
    return str(exc)


def _extract_meet_link(item: Dict[str, Any]) -> Optional[str]:
    """Extract a Google Meet link from a raw API event item."""
    link = item.get("hangoutLink")
    if not link:
        for ep in (item.get("conferenceData") or {}).get("entryPoints", []):
            if ep.get("entryPointType") == "video":
                return ep.get("uri")
    return link


def _meet_conference_body() -> Dict[str, Any]:
    """Conference data payload that tells Google to auto-create a Meet link."""
    return {
        "createRequest": {
            "requestId": uuid4().hex,
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }
    }


def _is_relevant(item: Dict[str, Any]) -> bool:
    """Filter out cancelled and self-declined events."""
    if item.get("status") == "cancelled":
        return False
    for att in item.get("attendees", []):
        if att.get("self") and att.get("responseStatus") == "declined":
            return False
    return True


def _parse_event(item: Dict[str, Any]) -> CalendarEvent:
    """Parse a raw Google Calendar API item into a CalendarEvent."""
    start_raw = item.get("start", {})
    end_raw = item.get("end", {})
    is_all_day = "date" in start_raw and "dateTime" not in start_raw

    attendees = [
        addr for a in item.get("attendees", [])
        if not a.get("self")
        and (addr := a.get("email") or a.get("displayName", ""))
    ]

    duration_minutes: Optional[int] = None
    if not is_all_day:
        start_str = start_raw.get("dateTime", "")
        end_str = end_raw.get("dateTime", "")
        if start_str and end_str:
            try:
                dt_start = datetime.fromisoformat(start_str)
                dt_end = datetime.fromisoformat(end_str)
                duration_minutes = max(0, int((dt_end - dt_start).total_seconds() // 60))
            except (ValueError, TypeError):
                pass

    series_id = item.get("recurringEventId")
    recurrence = _normalize_google_recurrence(item.get("recurrence"))
    if series_id:
        scope = "occurrence"
    elif recurrence is not None or item.get("recurrence"):
        scope = "series"
        series_id = item.get("id")
    else:
        scope = "event"
    return CalendarEvent(
        id=item["id"],
        title=item.get("summary", "(No title)"),
        start=start_raw.get("dateTime") or start_raw.get("date", ""),
        end=end_raw.get("dateTime") or end_raw.get("date", ""),
        location=item.get("location"),
        description=item.get("description"),
        is_all_day=is_all_day,
        attendees=attendees,
        attendee_count=len(attendees),
        duration_minutes=duration_minutes,
        meet_link=_extract_meet_link(item),
        scope=scope,
        series_id=series_id,
        recurrence=recurrence,
    )


def _dt_field(iso: str, tz_name: Optional[str]) -> Dict[str, Any]:
    """Build Google's {dateTime, timeZone?} field."""
    field: Dict[str, Any] = {"dateTime": iso}
    if tz_name:
        field["timeZone"] = tz_name
    return field


def _duration_minutes_from_google_event(item: Dict[str, Any]) -> Optional[int]:
    return duration_minutes_between(
        (item.get("start") or {}).get("dateTime"),
        (item.get("end") or {}).get("dateTime"),
    )


class GoogleProvider(_ProviderBase):
    """Google Calendar implementation of CalendarProvider."""

    name: str = "google"

    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client)
        self._calendar_ids_cache: Optional[List[str]] = None
        self._calendar_names: Dict[str, str] = {}

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        resp = await self._client.request(method, url, **kwargs)
        if resp.status_code == 401:
            await self.refresh()
            resp = await self._client.request(method, url, **kwargs)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise NeedsReauth("google") from e
            raise
        return resp

    async def list_calendar_ids(self) -> List[str]:
        """Discover all user-visible calendar IDs (cached for session lifetime)."""
        if self._calendar_ids_cache is not None:
            return self._calendar_ids_cache
        try:
            resp = await self._request(
                "GET",
                "/users/me/calendarList",
                params={"fields": "items(id,selected,summary)"},
            )
            items = resp.json().get("items", [])
            self._calendar_names = {
                item["id"]: (item.get("summary") or item["id"])
                for item in items
                if item.get("id") and item.get("selected", True)
            }
            self._calendar_ids_cache = list(self._calendar_names)
            logger.info("Discovered %d Google calendars", len(self._calendar_ids_cache))
            return self._calendar_ids_cache
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                logger.warning(
                    "calendarList returned 403 — missing calendarlist.readonly scope or Calendar API not enabled. "
                    "Falling back to primary."
                )
            else:
                logger.warning("calendarList failed (%s), falling back to primary", e)
            self._calendar_names = {"primary": "primary"}
            self._calendar_ids_cache = ["primary"]
            return self._calendar_ids_cache
        except NeedsReauth:
            raise
        except Exception as e:
            logger.warning("calendarList failed, falling back to primary: %s", e)
            self._calendar_names = {"primary": "primary"}
            self._calendar_ids_cache = ["primary"]
            return self._calendar_ids_cache

    async def list_events(
        self,
        time_min: str,
        time_max: str,
        max_results: int = 50,
    ) -> ProviderEventBatch:
        """Fetch events from all visible calendars, merged and deduplicated."""
        cal_ids = await self.list_calendar_ids()

        async def _query(cal_id: str) -> List[CalendarEvent]:
            resp = await self._request(
                "GET",
                f"/calendars/{_urlquote(cal_id, safe='')}/events",
                params={
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": True,
                    "orderBy": "startTime",
                    "maxResults": max_results,
                    "fields": _EVENT_FIELDS,
                },
            )
            title = self._calendar_names.get(cal_id)
            return [
                self._stamp(_parse_event(i), calendar=title)
                for i in resp.json().get("items", [])
                if _is_relevant(i)
            ]

        results = await asyncio.gather(
            *[_query(cid) for cid in cal_ids], return_exceptions=True,
        )

        all_events: List[CalendarEvent] = []
        errors: List[Exception] = []
        for r in results:
            if isinstance(r, list):
                all_events.extend(r)
            elif isinstance(r, Exception):
                errors.append(r)
                logger.warning("Google calendar query failed: %s", _describe_error(r))
        if errors and len(errors) == len(results):
            raise errors[0]

        seen: set[str] = set()
        unique = [ev for ev in all_events if ev.id not in seen and not seen.add(ev.id)]
        unique.sort(key=lambda e: e.start)
        return ProviderEventBatch(
            events=unique[:max_results],
            incomplete=bool(errors),
        )

    async def get_event(self, event_id: str) -> CalendarEvent:
        """Fetch full event details, searching across all visible calendars if needed."""
        try:
            cal_ids = await self.list_calendar_ids()
        except NeedsReauth:
            raise
        except Exception:
            cal_ids = ["primary"]

        last_exc: Exception | None = None
        for cid in cal_ids:
            try:
                resp = await self._request(
                    "GET",
                    f"/calendars/{_urlquote(cid, safe='')}/events/{_urlquote(event_id, safe='')}",
                    params={"fields": _SINGLE_EVENT_FIELDS},
                )
                return self._stamp(
                    _parse_event(resp.json()),
                    calendar=self._calendar_names.get(cid),
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    last_exc = e
                    continue
                raise
        raise last_exc or RuntimeError(f"Event {event_id!r} not found in any Google calendar")

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
    ) -> EventConfirmation:
        """Create an event on this provider's primary calendar."""
        if all_day:
            date_str = start[:10]
            end_str = end[:10] if end else ""
            if not end_str:
                end_dt = datetime.fromisoformat(date_str) + timedelta(days=1)
                end_str = end_dt.strftime("%Y-%m-%d")
            body: Dict[str, Any] = {
                "summary": title,
                "start": {"date": date_str},
                "end": {"date": end_str},
            }
            start_iso, end_iso = date_str, end_str
        else:
            start_dt = datetime.fromisoformat(start)
            if end:
                end_dt = datetime.fromisoformat(end)
            else:
                end_dt = start_dt + timedelta(minutes=duration_minutes)
            start_iso = start_dt.isoformat()
            end_iso = end_dt.isoformat()
            body = {
                "summary": title,
                "start": _dt_field(start_iso, tz_name),
                "end": _dt_field(end_iso, tz_name),
            }

        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]
        if add_meet:
            body["conferenceData"] = _meet_conference_body()
        if recurrence is not None:
            body["recurrence"] = [_google_rrule(recurrence, start_iso)]

        params: Dict[str, Any] = {}
        if add_meet:
            params["conferenceDataVersion"] = 1
        if attendees:
            params["sendUpdates"] = "all"

        response = await self._request(
            "POST", "/calendars/primary/events", json=body, params=params or None,
        )
        return _confirmation_from_google_item(
            response.json(),
            account=self.name,
            fallback_title=title,
            fallback_start=start_iso,
            fallback_end=end_iso,
        )

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
    ) -> EventConfirmation:
        """Patch an existing event."""
        body: Dict[str, Any] = {}

        if title is not None:
            body["summary"] = title
        if description is not None:
            body["description"] = description
        if location is not None:
            body["location"] = location

        if start is not None:
            start_dt = datetime.fromisoformat(start)
            body["start"] = _dt_field(start_dt.isoformat(), tz_name)

            if end is not None:
                end_dt = datetime.fromisoformat(end)
            elif duration_minutes is not None:
                end_dt = start_dt + timedelta(minutes=duration_minutes)
            else:
                existing = await self._request(
                    "GET",
                    f"/calendars/primary/events/{event_id}",
                    params={"fields": "start,end"},
                )
                current_duration = _duration_minutes_from_google_event(existing.json())
                end_dt = start_dt + timedelta(minutes=current_duration or 30)
            body["end"] = _dt_field(end_dt.isoformat(), tz_name)
        elif duration_minutes is not None:
            existing = await self._request("GET", f"/calendars/primary/events/{event_id}")
            orig_start = existing.json().get("start", {}).get("dateTime", "")
            if orig_start:
                start_dt = datetime.fromisoformat(orig_start)
                end_dt = start_dt + timedelta(minutes=duration_minutes)
                body["end"] = _dt_field(end_dt.isoformat(), tz_name)
        elif end is not None:
            end_dt = datetime.fromisoformat(end)
            body["end"] = _dt_field(end_dt.isoformat(), tz_name)

        if attendees is not None:
            body["attendees"] = [{"email": e} for e in attendees]
        if add_meet:
            body["conferenceData"] = _meet_conference_body()
        if recurrence is not None:
            recurrence_start = start or ""
            if not recurrence_start:
                existing = await self._request(
                    "GET",
                    f"/calendars/primary/events/{event_id}",
                    params={"fields": "start"},
                )
                start_raw = existing.json().get("start", {})
                recurrence_start = start_raw.get("dateTime") or start_raw.get("date", "")
            body["recurrence"] = [_google_rrule(recurrence, recurrence_start)]

        if not body:
            return EventConfirmation(
                id=event_id, title="(no changes)", start="", end="", account=self.name,
            )

        params: Dict[str, Any] = {}
        if add_meet:
            params["conferenceDataVersion"] = 1
        if attendees is not None:
            params["sendUpdates"] = "all"

        resp = await self._request(
            "PATCH",
            f"/calendars/primary/events/{event_id}",
            json=body,
            params=params or None,
        )
        return _confirmation_from_google_item(resp.json(), account=self.name)

    async def search_events(
        self,
        query: str,
        time_min: str,
        time_max: str,
        max_results: int = 20,
    ) -> ProviderEventBatch:
        """Full-text search across all visible calendars using Google's `q` param."""
        cal_ids = await self.list_calendar_ids()

        async def _query(cal_id: str) -> List[CalendarEvent]:
            resp = await self._request(
                "GET",
                f"/calendars/{_urlquote(cal_id, safe='')}/events",
                params={
                    "q": query,
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": True,
                    "orderBy": "startTime",
                    "maxResults": max_results,
                    "fields": _EVENT_FIELDS,
                },
            )
            title = self._calendar_names.get(cal_id)
            return [
                self._stamp(_parse_event(i), calendar=title)
                for i in resp.json().get("items", [])
                if _is_relevant(i)
            ]

        results = await asyncio.gather(
            *[_query(cid) for cid in cal_ids], return_exceptions=True,
        )

        all_events: List[CalendarEvent] = []
        errors: List[Exception] = []
        for r in results:
            if isinstance(r, list):
                all_events.extend(r)
            elif isinstance(r, Exception):
                errors.append(r)
                logger.warning(
                    "Google search_events failed for calendar: %s",
                    _describe_error(r),
                )
        if errors and len(errors) == len(results):
            raise errors[0]

        seen: set[str] = set()
        unique = [ev for ev in all_events if ev.id not in seen and not seen.add(ev.id)]
        unique.sort(key=lambda e: e.start)
        return ProviderEventBatch(
            events=unique[:max_results],
            incomplete=bool(errors),
        )

    async def delete_event(self, event_id: str) -> str | CapabilityErrorDetail:
        """Delete an event by id."""
        try:
            resp = await self._request("GET", f"/calendars/primary/events/{event_id}")
            summary = resp.json().get("summary", event_id)
        except httpx.HTTPStatusError:
            return CapabilityErrorDetail(
                code="not_found",
                message=f"Event '{event_id}' not found.",
            )

        await self._request("DELETE", f"/calendars/primary/events/{event_id}")
        return f"Deleted '{summary}' from your calendar."

    async def refresh(self) -> None:
        """Refresh the Google OAuth token and update the Authorization header."""
        token = await auth_manager.get_token("google")
        self._client.headers["Authorization"] = f"Bearer {token.access_token}"


def create_google_client(token_access_token: str) -> httpx.AsyncClient:
    """Build the httpx.AsyncClient for the Google Calendar API."""
    return httpx.AsyncClient(
        base_url=CALENDAR_API_BASE,
        headers={"Authorization": f"Bearer {token_access_token}"},
        timeout=10.0,
    )
