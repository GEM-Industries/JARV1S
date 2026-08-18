"""
OutlookProvider — Microsoft Graph backend behind the UnifiedCalendarClient.

Uses the same httpx.AsyncClient pattern as GoogleProvider. Single scope
`Calendars.ReadWrite` covers list/get/create/update/delete on the user's
default + additional calendars. `isOnlineMeeting=true` +
`onlineMeetingProvider="teamsForBusiness"` maps to Google's `add_meet` flag.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx

from core.auth.manager import auth_manager
from core.context import ensure_aware
from core.plugins.capabilities import CapabilityErrorDetail
from core.time import duration_minutes_between

from plugins.calendar.models import CalendarEvent, CalendarRecurrence, EventConfirmation
from plugins.calendar.providers.base import ProviderEventBatch, _ProviderBase

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0/me"

OUTLOOK_CALENDAR_SCOPES = ["Calendars.ReadWrite"]

# Graph fields we request on reads — mirrors the _SINGLE_EVENT_FIELDS set
# used for Google, tailored to Graph's event shape.
_GRAPH_SELECT = (
    "id,subject,start,end,location,bodyPreview,isAllDay,isCancelled,"
    "attendees,onlineMeeting,showAs,responseStatus,webLink,type,seriesMasterId,recurrence"
)

_GRAPH_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _anchor_datetime(start: str) -> datetime:
    if len(start) == 10:
        return datetime.fromisoformat(f"{start}T00:00:00")
    return datetime.fromisoformat(start)


def _graph_recurrence_payload(
    recurrence: CalendarRecurrence,
    start: str,
    tz_name: Optional[str],
) -> Dict[str, Any]:
    anchor = _anchor_datetime(start)
    start_date = anchor.date().isoformat()
    if recurrence == "daily":
        pattern: Dict[str, Any] = {"type": "daily", "interval": 1}
    elif recurrence == "weekdays":
        pattern = {
            "type": "weekly",
            "interval": 1,
            "daysOfWeek": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        }
    elif recurrence == "weekly":
        pattern = {
            "type": "weekly",
            "interval": 1,
            "daysOfWeek": [_GRAPH_WEEKDAYS[anchor.weekday()]],
        }
    elif recurrence == "monthly":
        pattern = {
            "type": "absoluteMonthly",
            "interval": 1,
            "dayOfMonth": anchor.day,
        }
    elif recurrence == "yearly":
        pattern = {
            "type": "absoluteYearly",
            "interval": 1,
            "month": anchor.month,
            "dayOfMonth": anchor.day,
        }
    else:
        raise ValueError(f"Unsupported recurrence={recurrence!r}")

    range_body: Dict[str, Any] = {
        "type": "noEnd",
        "startDate": start_date,
    }
    if tz_name:
        range_body["recurrenceTimeZone"] = tz_name
    return {"pattern": pattern, "range": range_body}


def _normalize_graph_recurrence(raw: Any) -> Optional[CalendarRecurrence]:
    if not isinstance(raw, dict):
        return None
    pattern = raw.get("pattern") or {}
    pattern_type = (pattern.get("type") or "").lower()
    days = [str(day).lower() for day in (pattern.get("daysOfWeek") or [])]
    if pattern_type == "daily":
        return "daily"
    if pattern_type == "weekly":
        weekday_set = {"monday", "tuesday", "wednesday", "thursday", "friday"}
        if set(days) == weekday_set:
            return "weekdays"
        return "weekly"
    if pattern_type == "absolutemonthly":
        return "monthly"
    if pattern_type == "absoluteyearly":
        return "yearly"
    return None


def _confirmation_from_graph_item(
    item: Dict[str, Any],
    *,
    account: Optional[str],
    fallback_start: str = "",
    fallback_end: str = "",
) -> EventConfirmation:
    parsed = _parse_graph_event(item)
    return EventConfirmation(
        id=item["id"],
        title=parsed.title,
        start=parsed.start or fallback_start,
        end=parsed.end or fallback_end,
        html_link=item.get("webLink"),
        meet_link=parsed.meet_link,
        account=account,
        scope=parsed.scope,
        series_id=parsed.series_id or (item["id"] if parsed.scope == "series" else None),
        recurrence=parsed.recurrence,
    )


def _parse_graph_datetime(dt_obj: Dict[str, Any]) -> str:
    """Convert Graph {'dateTime': '...', 'timeZone': 'UTC'} into a tz-aware ISO string.

    Graph returns naive-looking `dateTime` with the zone in the sibling `timeZone`
    field. We don't send a `Prefer: outlook.timezone` header, so Graph always
    returns UTC and we can treat naive strings as UTC.
    """
    raw = (dt_obj or {}).get("dateTime", "")
    if not raw:
        return ""
    try:
        return ensure_aware(raw, ZoneInfo("UTC")).isoformat()
    except ValueError:
        return raw


def _parse_graph_event(item: Dict[str, Any]) -> CalendarEvent:
    """Parse a raw Microsoft Graph event into a CalendarEvent."""
    is_all_day = bool(item.get("isAllDay", False))

    start_iso = _parse_graph_datetime(item.get("start") or {})
    end_iso = _parse_graph_datetime(item.get("end") or {})

    if is_all_day:
        # Graph returns midnight UTC for all-day events; slice to YYYY-MM-DD
        # to match Google's date-only shape.
        start_out = start_iso[:10] if start_iso else ""
        end_out = end_iso[:10] if end_iso else ""
    else:
        start_out = start_iso
        end_out = end_iso

    # Attendees — Graph exposes them as {emailAddress: {address, name}, type, status}.
    # Match Google's "exclude self, prefer email" shape.
    attendees: List[str] = []
    for a in item.get("attendees", []):
        email_obj = a.get("emailAddress", {}) or {}
        addr = email_obj.get("address") or email_obj.get("name", "")
        # Graph doesn't flag "self" like Google — response "self" is on top-level
        # responseStatus, not inside attendees. We can't reliably drop the user's
        # own entry without an extra /me call, but Graph already omits the organizer
        # from the attendees list, so this mirrors the practical behavior.
        if addr:
            attendees.append(addr)

    duration_minutes: Optional[int] = None
    if not is_all_day and start_iso and end_iso:
        try:
            dt_start = ensure_aware(start_iso, ZoneInfo("UTC"))
            dt_end = ensure_aware(end_iso, ZoneInfo("UTC"))
            duration_minutes = max(0, int((dt_end - dt_start).total_seconds() // 60))
        except (ValueError, TypeError):
            pass

    online = item.get("onlineMeeting") or {}
    meet_link = online.get("joinUrl") if online else None

    location_obj = item.get("location") or {}
    location_name = location_obj.get("displayName") if location_obj else None

    event_type = item.get("type")
    series_id = item.get("seriesMasterId")
    recurrence = _normalize_graph_recurrence(item.get("recurrence"))
    scope = (
        "series"
        if event_type == "seriesMaster" or (recurrence is not None and not series_id)
        else "occurrence"
        if event_type in {"occurrence", "exception"} or series_id
        else "event"
    )
    if scope == "series" and not series_id:
        series_id = item.get("id")
    return CalendarEvent(
        id=item["id"],
        title=item.get("subject") or "(No title)",
        start=start_out,
        end=end_out,
        location=location_name or None,
        description=item.get("bodyPreview") or None,
        is_all_day=is_all_day,
        attendees=attendees,
        attendee_count=len(attendees),
        duration_minutes=duration_minutes,
        meet_link=meet_link,
        scope=scope,
        series_id=series_id,
        recurrence=recurrence,
    )


def _is_graph_relevant(item: Dict[str, Any]) -> bool:
    """Filter out cancelled and self-declined events."""
    if item.get("isCancelled"):
        return False
    response = (item.get("responseStatus") or {}).get("response")
    if response == "declined":
        return False
    return True


def _build_graph_dt(iso: str, tz_name: Optional[str]) -> Dict[str, str]:
    """Build Graph's {dateTime, timeZone} payload.

    Graph separates the time from its zone: `dateTime` must be naive and
    `timeZone` names the zone. Convert the incoming (assumed tz-aware) ISO
    into the target zone, then strip the offset.
    """
    tz_name = tz_name or "UTC"
    try:
        dt = ensure_aware(iso, ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))
        naive = dt.replace(tzinfo=None).isoformat()
    except (ValueError, KeyError):
        naive = iso
    return {"dateTime": naive, "timeZone": tz_name}


def _duration_minutes_from_graph_event(item: Dict[str, Any]) -> Optional[int]:
    return duration_minutes_between(
        _parse_graph_datetime(item.get("start") or {}),
        _parse_graph_datetime(item.get("end") or {}),
    )


def _escape_odata_string(value: str) -> str:
    return value.replace("'", "''")


class OutlookProvider(_ProviderBase):
    """Microsoft Graph implementation of CalendarProvider."""

    name: str = "microsoft"

    def __init__(self, client: httpx.AsyncClient, account: Optional[str] = None):
        super().__init__(client, account)
        self._calendar_ids_cache: Optional[List[str]] = None

    async def list_calendar_ids(self) -> Optional[List[str]]:
        """Discover the user's calendar IDs on Microsoft Graph (cached).

        Returns None if discovery fails or returns zero calendars — callers
        fall back to the default-calendar endpoint.
        """
        if self._calendar_ids_cache is not None:
            return self._calendar_ids_cache or None
        try:
            resp = await self._client.get("/calendars", params={"$select": "id,name"})
            resp.raise_for_status()
            items = resp.json().get("value", [])
            self._calendar_ids_cache = [item["id"] for item in items if item.get("id")]
            logger.info("Discovered %d Outlook calendars", len(self._calendar_ids_cache))
            return self._calendar_ids_cache or None
        except Exception as e:
            logger.warning("Graph calendars list failed, falling back to default: %s", e)
            return None

    async def list_events(
        self,
        time_min: str,
        time_max: str,
        max_results: int = 50,
    ) -> ProviderEventBatch:
        """Fan out events across user calendars via calendarView (time-expanded).

        If calendar discovery fails, hit /me/calendarView directly.
        """
        cal_ids = await self.list_calendar_ids()

        params_base = {
            "startDateTime": time_min,
            "endDateTime": time_max,
            "$select": _GRAPH_SELECT,
            "$orderby": "start/dateTime",
            "$top": max_results,
        }

        async def _query(path: str) -> List[CalendarEvent]:
            resp = await self._client.get(path, params=params_base)
            resp.raise_for_status()
            return [
                _parse_graph_event(i)
                for i in resp.json().get("value", [])
                if _is_graph_relevant(i)
            ]

        if cal_ids is None:
            paths = ["/calendarView"]
        else:
            paths = [f"/calendars/{cid}/calendarView" for cid in cal_ids]

        results = await asyncio.gather(
            *[_query(p) for p in paths], return_exceptions=True,
        )

        all_events: List[CalendarEvent] = []
        errors: List[Exception] = []
        for r in results:
            if isinstance(r, list):
                all_events.extend(r)
            elif isinstance(r, Exception):
                errors.append(r)
                logger.warning("Outlook calendar query failed: %s", r)
        if errors and len(errors) == len(results):
            raise errors[0]

        seen: set[str] = set()
        unique = [ev for ev in all_events if ev.id not in seen and not seen.add(ev.id)]
        unique.sort(key=lambda e: e.start)
        return ProviderEventBatch(
            events=[self._stamp(e) for e in unique[:max_results]],
            incomplete=bool(errors),
        )

    async def get_event(self, event_id: str) -> CalendarEvent:
        """Fetch a single Graph event by id."""
        resp = await self._client.get(
            f"/events/{event_id}", params={"$select": _GRAPH_SELECT},
        )
        resp.raise_for_status()
        return self._stamp(_parse_graph_event(resp.json()))

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
        """Create a Graph event on the default calendar."""
        if all_day:
            date_str = start[:10]
            end_str = end[:10] if end else ""
            if not end_str:
                end_dt = datetime.fromisoformat(date_str) + timedelta(days=1)
                end_str = end_dt.strftime("%Y-%m-%d")
            body: Dict[str, Any] = {
                "subject": title,
                "isAllDay": True,
                "start": {"dateTime": date_str + "T00:00:00", "timeZone": tz_name or "UTC"},
                "end": {"dateTime": end_str + "T00:00:00", "timeZone": tz_name or "UTC"},
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
                "subject": title,
                "start": _build_graph_dt(start_iso, tz_name),
                "end": _build_graph_dt(end_iso, tz_name),
            }

        if description:
            body["body"] = {"contentType": "text", "content": description}
        if location:
            body["location"] = {"displayName": location}
        if attendees:
            body["attendees"] = [
                {"emailAddress": {"address": e}, "type": "required"} for e in attendees
            ]
        if add_meet:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        if recurrence is not None:
            body["recurrence"] = _graph_recurrence_payload(recurrence, start_iso, tz_name)

        resp = await self._client.post("/events", json=body)
        resp.raise_for_status()
        return _confirmation_from_graph_item(
            resp.json(),
            account=self._account,
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
        """PATCH a Graph event."""
        body: Dict[str, Any] = {}

        if title is not None:
            body["subject"] = title
        if description is not None:
            body["body"] = {"contentType": "text", "content": description}
        if location is not None:
            body["location"] = {"displayName": location}

        if start is not None:
            start_dt = datetime.fromisoformat(start)
            body["start"] = _build_graph_dt(start_dt.isoformat(), tz_name)

            if end is not None:
                end_dt = datetime.fromisoformat(end)
            elif duration_minutes is not None:
                end_dt = start_dt + timedelta(minutes=duration_minutes)
            else:
                existing = await self._client.get(
                    f"/events/{event_id}", params={"$select": "start,end"},
                )
                existing.raise_for_status()
                current_duration = _duration_minutes_from_graph_event(existing.json())
                end_dt = start_dt + timedelta(minutes=current_duration or 30)
            body["end"] = _build_graph_dt(end_dt.isoformat(), tz_name)
        elif duration_minutes is not None:
            existing = await self._client.get(
                f"/events/{event_id}", params={"$select": "start,end"},
            )
            existing.raise_for_status()
            orig_start_raw = _parse_graph_datetime(existing.json().get("start") or {})
            if orig_start_raw:
                try:
                    start_dt = datetime.fromisoformat(orig_start_raw.replace("Z", "+00:00"))
                    end_dt = start_dt + timedelta(minutes=duration_minutes)
                    body["end"] = _build_graph_dt(end_dt.isoformat(), tz_name)
                except ValueError:
                    pass
        elif end is not None:
            end_dt = datetime.fromisoformat(end)
            body["end"] = _build_graph_dt(end_dt.isoformat(), tz_name)

        if attendees is not None:
            body["attendees"] = [
                {"emailAddress": {"address": e}, "type": "required"} for e in attendees
            ]
        if add_meet:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"
        if recurrence is not None:
            recurrence_start = start or ""
            if not recurrence_start:
                existing = await self._client.get(
                    f"/events/{event_id}", params={"$select": "start,isAllDay"},
                )
                existing.raise_for_status()
                payload = existing.json()
                if payload.get("isAllDay"):
                    recurrence_start = _parse_graph_datetime(payload.get("start") or {})[:10]
                else:
                    recurrence_start = _parse_graph_datetime(payload.get("start") or {})
            body["recurrence"] = _graph_recurrence_payload(
                recurrence, recurrence_start, tz_name,
            )

        if not body:
            return EventConfirmation(
                id=event_id, title="(no changes)", start="", end="", account=self._account,
            )

        resp = await self._client.patch(f"/events/{event_id}", json=body)
        resp.raise_for_status()
        return _confirmation_from_graph_item(resp.json(), account=self._account)

    async def search_events(
        self,
        query: str,
        time_min: str,
        time_max: str,
        max_results: int = 20,
    ) -> ProviderEventBatch:
        """Search Graph calendarView with a server-side subject filter."""
        cal_ids = await self.list_calendar_ids()
        params = {
            "startDateTime": time_min,
            "endDateTime": time_max,
            "$filter": f"contains(subject,'{_escape_odata_string(query)}')",
            "$select": _GRAPH_SELECT,
            "$top": max_results,
        }

        async def _query(path: str) -> List[CalendarEvent]:
            resp = await self._client.get(
                path,
                params=params,
                headers={"ConsistencyLevel": "eventual"},
            )
            resp.raise_for_status()
            return [
                _parse_graph_event(i)
                for i in resp.json().get("value", [])
                if _is_graph_relevant(i)
            ]

        if cal_ids is None:
            paths = ["/calendarView"]
        else:
            paths = [f"/calendars/{cid}/calendarView" for cid in cal_ids]

        results = await asyncio.gather(
            *[_query(p) for p in paths], return_exceptions=True,
        )
        all_events: List[CalendarEvent] = []
        errors: List[Exception] = []
        for r in results:
            if isinstance(r, list):
                all_events.extend(r)
            elif isinstance(r, Exception):
                errors.append(r)
                logger.warning("Outlook search_events failed: %s", r)
        if errors and len(errors) == len(results):
            raise errors[0]

        seen: set[str] = set()
        unique = [ev for ev in all_events if ev.id not in seen and not seen.add(ev.id)]
        unique.sort(key=lambda e: e.start)
        return ProviderEventBatch(
            events=[self._stamp(e) for e in unique[:max_results]],
            incomplete=bool(errors),
        )

    async def delete_event(self, event_id: str) -> str | CapabilityErrorDetail:
        """DELETE a Graph event by id."""
        try:
            resp = await self._client.get(
                f"/events/{event_id}", params={"$select": "subject"},
            )
            resp.raise_for_status()
            summary = resp.json().get("subject", event_id)
        except httpx.HTTPStatusError:
            return CapabilityErrorDetail(
                code="not_found",
                message=f"Event '{event_id}' not found.",
            )

        del_resp = await self._client.delete(f"/events/{event_id}")
        # Graph returns 204 No Content on success
        if del_resp.status_code not in (200, 204):
            del_resp.raise_for_status()
        return f"Deleted '{summary}' from your Outlook calendar."

    async def refresh(self) -> None:
        """Refresh the Microsoft OAuth token and update the Authorization header."""
        token = await auth_manager.get_token("microsoft")
        self._client.headers["Authorization"] = f"Bearer {token.access_token}"


def create_outlook_client(token_access_token: str) -> httpx.AsyncClient:
    """Build the httpx.AsyncClient for Microsoft Graph."""
    return httpx.AsyncClient(
        base_url=GRAPH_API_BASE,
        headers={"Authorization": f"Bearer {token_access_token}"},
        timeout=10.0,
    )
