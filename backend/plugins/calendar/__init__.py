"""
Calendar Plugin for JARV1S.

Multi-provider aware: injects a UnifiedCalendarClient that wraps EventKit on
this Mac and/or Google Calendar and Microsoft Graph. The LLM sees a single
calendar; writes are routed by connection name (`google` | `microsoft` |
`macos`). EventKit is read-only in V0.

Provider-specific parsing, URL shaping, and Meet/Teams payload construction live
inside plugins/calendar/providers/ behind the CalendarProvider Protocol.
"""

import logging
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Literal, Optional

from core.context import ensure_aware, get_tz, to_local_iso
from core.decorators import tool
from core.plugins.consent import require_consent
from core.plugins.result import ToolResult
from core.plugins.types import JarvisPlugin, PluginMetadata, UIEnvelope, WidgetLayout, WidgetSize
from core.plugins.ui import push_content, push_ui
from core.time import parse_datetime, parse_duration
from core.plugins.capabilities import CapabilityErrorDetail

from plugins.calendar.models import (
    CalendarEvent,
    CalendarQueryResult,
    CalendarRecurrence,
    EventConfirmation,
    TimeSlot,
)
from plugins.calendar.unified import UnifiedCalendarClient


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)

logger = logging.getLogger(__name__)


__all__ = [
    "CalendarEvent",
    "EventConfirmation",
    "TimeSlot",
    "CalendarPlugin",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _query_events(result: CalendarQueryResult | list[CalendarEvent]) -> list[CalendarEvent]:
    if isinstance(result, CalendarQueryResult):
        return list(result.events)
    return list(result)


def _calendar_day_envelope(day: datetime, events: list[CalendarEvent]) -> UIEnvelope:
    return UIEnvelope(
        widget_id=f"calendar-{day.strftime('%Y-%m-%d')}",
        component="CalendarWidget",
        title=f"Calendar — {day.strftime('%A, %B %-d')}",
        layout=WidgetLayout(size=WidgetSize.LARGE_WIDE, priority=4),
        data={
            "date": day.strftime("%Y-%m-%d"),
            "events": [e.model_dump() for e in events],
            "event_count": len(events),
        },
    )


def _localize_event(event: CalendarEvent, tz: zoneinfo.ZoneInfo) -> CalendarEvent:
    """Convert start/end to the user's local timezone for LLM presentation.
    All-day events (date-only strings) are returned unchanged.
    """
    if event.is_all_day:
        return event
    return event.model_copy(update={
        "start": to_local_iso(event.start, tz),
        "end": to_local_iso(event.end, tz),
    })


def _tz() -> zoneinfo.ZoneInfo:
    return get_tz()


def _parse_calendar_datetime(value: str, tz: zoneinfo.ZoneInfo) -> datetime:
    return parse_datetime(value, timezone_name=str(tz)).astimezone(tz)


def _parse_calendar_date(value: str, tz: zoneinfo.ZoneInfo, now: datetime) -> datetime:
    parsed = parse_datetime(
        value,
        now=now.astimezone(timezone.utc),
        timezone_name=str(tz),
        default_time_for_date=datetime.min.time(),
    )
    local = parsed.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _coerce_duration_minutes(value: int | str) -> int:
    if isinstance(value, int):
        return value
    now = datetime.now(timezone.utc)
    parsed = parse_duration(value, now=now)
    if parsed is None:
        raise ValueError(f"Invalid duration_minutes={value!r}. Use an integer or duration like '30m' or '1.5h'.")
    return max(1, int(round((parsed - now).total_seconds() / 60)))


def _calendar_delete_when(event: CalendarEvent, tz: zoneinfo.ZoneInfo) -> str:
    if event.is_all_day:
        return event.start
    dt = ensure_aware(event.start, tz).astimezone(tz)
    clock = dt.strftime("%I:%M %p").lstrip("0").lower()
    return f"{dt.strftime('%A')} at {clock}"


def _event_review_sections(event: EventConfirmation) -> list[dict[str, Any]]:
    pairs = {
        "Event ID": event.id,
        "Title": event.title,
        "Start": event.start,
        "End": event.end,
    }
    if event.account:
        pairs["Account"] = event.account
    sections: list[dict[str, Any]] = [{"type": "kv", "pairs": pairs}]
    if event.meet_link:
        sections.append({"type": "markdown", "content": f"Meeting link: {event.meet_link}"})
    if event.conflicts:
        sections.append({
            "type": "list",
            "items": [str(conflict) for conflict in event.conflicts],
            "ordered": False,
        })
    return sections


def _expected_start_matches(actual: str, expected: str, tz: zoneinfo.ZoneInfo) -> bool:
    try:
        actual_dt = ensure_aware(actual, tz)
        expected_dt = ensure_aware(expected, tz)
        return actual_dt.replace(second=0, microsecond=0) == expected_dt.replace(second=0, microsecond=0)
    except Exception:
        return actual.startswith(expected) or expected.startswith(actual)


def _delete_guard_error(
    event: CalendarEvent,
    *,
    expected_title: str | None,
    expected_start: str | None,
    expected_account: str | None,
    tz: zoneinfo.ZoneInfo,
) -> CapabilityErrorDetail | None:
    if expected_title and expected_title.lower() not in event.title.lower():
        return _fail(
            "Refusing to delete; target title did not match. "
            f"Fetched '{event.title}', expected '{expected_title}'."
        )
    if expected_start and not _expected_start_matches(event.start, expected_start, tz):
        return _fail(
            "Refusing to delete; target start did not match. "
            f"Fetched '{event.start}', expected '{expected_start}'."
        )
    if expected_account and event.account and event.account != expected_account:
        return _fail(
            "Refusing to delete; target account did not match. "
            f"Fetched '{event.account}', expected '{expected_account}'."
        )
    return None


def _events_overlap(
    first_start: str,
    first_end: str,
    second_start: str,
    second_end: str,
    tz: zoneinfo.ZoneInfo,
) -> bool:
    try:
        first_start_dt = ensure_aware(first_start, tz)
        first_end_dt = ensure_aware(first_end, tz)
        second_start_dt = ensure_aware(second_start, tz)
        second_end_dt = ensure_aware(second_end, tz)
        return first_start_dt < second_end_dt and second_start_dt < first_end_dt
    except Exception:
        return False


def _is_exact_duplicate_event(
    event: CalendarEvent,
    *,
    title: str,
    start: str,
    end: str,
    account: str | None,
    tz: zoneinfo.ZoneInfo,
) -> bool:
    if event.title.strip().casefold() != title.strip().casefold():
        return False
    if account and event.account and event.account != account:
        return False
    return (
        _expected_start_matches(event.start, start, tz)
        and _expected_start_matches(event.end, end, tz)
    )


def _is_likely_duplicate_event(
    event: CalendarEvent,
    *,
    title: str,
    start: str,
    end: str,
    account: str | None,
    tz: zoneinfo.ZoneInfo,
) -> bool:
    if _is_exact_duplicate_event(
        event,
        title=title,
        start=start,
        end=end,
        account=account,
        tz=tz,
    ):
        return True
    if event.title.strip().casefold() != title.strip().casefold():
        return False
    if account and event.account and event.account != account:
        return False
    return _events_overlap(event.start, event.end, start, end, tz)


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class CalendarPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="calendar",
        version="2.0.0",
        description=(
            "Calendar: schedule queries, event CRUD, attendees, conferencing links "
            "(Meet/Teams), free-time discovery. Connections: this Mac, Google, Microsoft."
        ),
        dependencies=["httpx"],
        utterances=[
            "what's on my calendar today",
            "do I have any meetings this week",
            "what's my schedule on Thursday",
            "when are my exams",
            "when is my appointment",
            "check my appointment",
            "am I busy tomorrow afternoon",
            "schedule a call with Dave at 3pm",
            "book a meeting for Friday at 2",
            "when am I free tomorrow",
            "is my evening free on Tuesday",
            "cancel my afternoon appointment",
            "delete that meeting",
            "move my 3pm to 4",
            "reschedule standup to Thursday",
            "add Sarah to the meeting",
            "set up a meeting with a Google Meet link",
            "who's on my next call",
            "what's the Meet link for standup",
            "block off Friday",
            "put it on my work calendar",
            "add it to my personal calendar",
        ],
    )

    async def register_integrations(self) -> None:
        from core.integrations import integrations
        from plugins.calendar.providers.google import GOOGLE_CALENDAR_SCOPES
        from plugins.calendar.providers.outlook import OUTLOOK_CALENDAR_SCOPES
        from plugins.calendar.unified import (
            create_calendar_client,
            refresh_calendar_client,
        )

        # Register without a primary provider — the factory handles per-provider
        # scope validation itself (see plugins.calendar.unified.build_unified_client).
        integrations.register(
            "calendar",
            create_calendar_client,
            refresh=refresh_calendar_client,
        )
        # Declare scopes for both providers so OAuth consent screens include them.
        integrations.register_aux_provider_scopes(
            "google",
            GOOGLE_CALENDAR_SCOPES,
            integration_name="calendar",
        )
        integrations.register_aux_provider_scopes(
            "microsoft",
            OUTLOOK_CALENDAR_SCOPES,
            integration_name="calendar",
        )

    # -----------------------------------------------------------------------
    # Tools
    # -----------------------------------------------------------------------

    @tool(inject=["calendar"])
    async def get_events(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        calendar: UnifiedCalendarClient = None,
    ) -> List[CalendarEvent]:
        """
        Get calendar events for a date range. Dates accept ISO or natural dates like "tomorrow".
        Defaults to today if no dates given. If only start_date given, returns that single day.
        Returns events from ALL connected calendars merged together — each event carries an
        `account` field (`google`, `microsoft`, or `macos`) so you can pass it back for mutations.
        ALWAYS resolve day names using "Week Dates" from the system prompt context — never compute offsets manually.
        Query multiple days in one call rather than asking the user to pick one day at a time.
        VOICE: Narrate events as flowing prose, not a list. Weave them together with commas and connectors.
        Naturalize event titles — use the person or purpose as the anchor, drop formulaic prefixes like "Weekly" or "Q2".
        Use spoken times — never read ISO timestamps. Use natural forms like "half six" or "quarter past two".
        Do NOT mention the connection (`google` / `microsoft` / `macos`) unless the user asks — just answer the question.
        Zero events: say the day is clear. One event: name it naturally with the time.
        Several: combine into one flowing sentence. Do not enumerate or number them.
        Multi-day: group by day with natural transitions between them.
        """
        tz = _tz()
        now = datetime.now(tz)

        if start_date:
            range_start = _parse_calendar_date(start_date, tz, now)
        else:
            range_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if end_date:
            range_end = _parse_calendar_date(end_date, tz, now) + timedelta(days=1)
        else:
            range_end = range_start + timedelta(days=1)

        window_days = max(1, (range_end - range_start).days)
        max_results = min(500, max(50, window_days * 50))
        events = await calendar.list_events(
            range_start.isoformat(), range_end.isoformat(), max_results=max_results,
        )
        localized = [_localize_event(e, tz) for e in _query_events(events)]
        if window_days == 1:
            push_ui(_calendar_day_envelope(range_start, localized))
        return localized

    @tool(inject=["calendar"])
    async def get_event(
        self,
        event_id: str,
        account: Optional[str] = None,
        calendar: UnifiedCalendarClient = None,
    ) -> CalendarEvent:
        """
        Fetch full details for a single event by its ID, including attendees.
        Use when get_events() returns attendees=[] for a meeting — this fetches the
        canonical record which may include attendee data unavailable in list results.
        event_id comes from the id field of a CalendarEvent returned by get_events().
        account: Pass through the `account` field from the CalendarEvent (`google`, `microsoft`, or `macos`).
        Omit only if you don't know which connection it's on
        (we'll try each connected calendar in turn, slower).
        If attendees is still empty after calling this, tell the user rather than guessing.
        VOICE: Speak the attendees naturally by first name. If none, say you can't see who's invited.
        """
        tz = _tz()
        event = await calendar.get_event(event_id, account=account)
        return _localize_event(event, tz)

    @tool(inject=["calendar"])
    async def create_event(
        self,
        title: str,
        start: str,
        duration_minutes: int | str = 30,
        end: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[List[str]] = None,
        add_meet: bool = False,
        all_day: bool = False,
        recurrence: Optional[CalendarRecurrence] = None,
        account: Optional[str] = None,
        calendar: UnifiedCalendarClient = None,
    ) -> EventConfirmation | CapabilityErrorDetail:
        """
        Create a calendar event. start accepts ISO or natural local time like "tomorrow 3pm" (date for all_day=true).
        end defaults to start + duration_minutes (30, "90m", or "1.5h"). attendees is a list of email addresses.
        add_meet=true generates a video meeting link (Google Meet on google, Teams on microsoft).
        all_day=true creates an all-day event.
        account: Connection to write to (`google` or `microsoft`). Default is the sole writable
        connection when only one of those is connected. macos cannot create events.
        REQUIRED: Always ask for a title first — never invent one. Always ask for email addresses before adding attendees.
        If changing a just-created event, use update_event with the returned id; do not call create_event again.
        If this returns an existing same-title overlapping event, treat it as a likely duplicate and ask/update instead of creating another.
        VOICE: Warn naturally about conflicts. Confirm briefly with title, day, spoken time.
        """
        tz = _tz()
        tz_name = str(tz)
        duration_mins = _coerce_duration_minutes(duration_minutes)

        # Normalize timezone-naive inputs to the user's tz so provider payloads stay consistent.
        if not all_day:
            start_dt = _parse_calendar_datetime(start, tz)
            end_dt = _parse_calendar_datetime(end, tz) if end else start_dt + timedelta(minutes=duration_mins)
            start, end = start_dt.isoformat(), end_dt.isoformat()

            # Conflict detection fans out across all connected providers silently.
            raw_conflicts = await calendar.list_events(start, end)
            conflicts = [_localize_event(e, tz) for e in _query_events(raw_conflicts)]
            duplicate = next(
                (
                    event for event in conflicts
                    if _is_likely_duplicate_event(
                        event,
                        title=title,
                        start=start,
                        end=end,
                        account=account,
                        tz=tz,
                    )
                ),
                None,
            )
            if duplicate:
                return EventConfirmation(
                    id=duplicate.id,
                    title=duplicate.title,
                    start=duplicate.start,
                    end=duplicate.end,
                    account=duplicate.account,
                    conflicts=conflicts,
                )
        else:
            start_local = _parse_calendar_date(start, tz, datetime.now(tz))
            end_local = (
                _parse_calendar_date(end, tz, datetime.now(tz))
                if end
                else start_local + timedelta(days=1)
            )
            start = start_local.date().isoformat()
            end = end_local.date().isoformat()
            raw_conflicts = await calendar.list_events(
                start_local.isoformat(),
                end_local.isoformat(),
            )
            conflicts = [_localize_event(e, tz) for e in _query_events(raw_conflicts)]
            duplicate = next(
                (
                    event for event in conflicts
                    if _is_likely_duplicate_event(
                        event,
                        title=title,
                        start=start,
                        end=end,
                        account=account,
                        tz=tz,
                    )
                ),
                None,
            )
            if duplicate:
                return EventConfirmation(
                    id=duplicate.id,
                    title=duplicate.title,
                    start=duplicate.start,
                    end=duplicate.end,
                    account=duplicate.account,
                    conflicts=conflicts,
                )

        confirmation = await calendar.create_event(
            title=title,
            start=start,
            duration_minutes=duration_mins,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
            add_meet=add_meet,
            all_day=all_day,
            recurrence=recurrence,
            tz_name=tz_name,
            account=account,
        )
        if isinstance(confirmation, CapabilityErrorDetail):
            return confirmation
        confirmation = confirmation.model_copy(update={"conflicts": conflicts})
        push_content(
            title="Calendar Event Created",
            sections=_event_review_sections(confirmation),
            size=WidgetSize.LARGE_WIDE,
        )
        return confirmation

    @tool(inject=["calendar"])
    async def delete_event(
        self,
        event_id: str,
        account: Optional[str] = None,
        expected_title: Optional[str] = None,
        expected_start: Optional[str] = None,
        expected_account: Optional[str] = None,
        calendar: UnifiedCalendarClient = None,
    ) -> ToolResult | CapabilityErrorDetail:
        """
        Delete a calendar event by ID. Has built-in approval — call immediately, do NOT ask first.
        Get the event_id from get_events() first if the user refers to an event by name.
        account: Pass through the `account` field from the CalendarEvent you read (`google`, `microsoft`, or `macos`).
        Omit only if exactly one calendar account is connected.
        For risky follow-up deletes, pass expected_title / expected_start / expected_account from the event you meant to delete; the tool refuses if the fetched target differs.
        """
        tz = _tz()
        try:
            target = await calendar.get_event(event_id, account=account)
            target = _localize_event(target, tz)
        except Exception as exc:
            return _fail(f"Could not resolve calendar event; no approval was created. Details: {exc}")

        guard_error = _delete_guard_error(
            target,
            expected_title=expected_title,
            expected_start=expected_start,
            expected_account=expected_account,
            tz=tz,
        )
        if guard_error:
            return guard_error

        async def _do_delete() -> ToolResult | CapabilityErrorDetail:
            result = await calendar.delete_event(event_id, account=target.account or account)
            if isinstance(result, CapabilityErrorDetail):
                return result
            return ToolResult(content=f'Deleted "{target.title}".')

        when = _calendar_delete_when(target, tz)
        target_account = target.account or account or "auto"
        description = f'Delete "{target.title}" on {when}?'
        return await require_consent(
            description,
            _do_delete,
            detail=(
                f"Event ID: {event_id}\n"
                f"Account: {target_account}\n"
                f"Title: {target.title}\n"
                f"Start: {target.start}\n"
                f"End: {target.end}"
            ),
        )

    @tool(inject=["calendar"])
    async def update_event(
        self,
        event_id: str,
        title: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        duration_minutes: Optional[int | str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        attendees: Optional[List[str]] = None,
        add_meet: bool = False,
        recurrence: Optional[CalendarRecurrence] = None,
        scope: Optional[Literal["event", "occurrence", "series"]] = None,
        account: Optional[str] = None,
        expected_title: Optional[str] = None,
        expected_start: Optional[str] = None,
        expected_account: Optional[str] = None,
        calendar: UnifiedCalendarClient = None,
    ) -> EventConfirmation | CapabilityErrorDetail:
        """
        Update an existing calendar event (PATCH — only supplied fields change).
        Get event_id from get_events() first if the user refers to an event by name.
        account: Pass through the `account` field from the CalendarEvent you read (`google`, `microsoft`, or `macos`).
        Do not guess — the event lives on one connection, pass the account it came from.
        Pass expected_title / expected_start / expected_account when the ID comes from a prior lookup; the tool refuses if the fetched target differs.
        Recurring targets require explicit scope='occurrence' or scope='series'. Changing recurrence requires scope='series'.
        attendees replaces the full list — include ALL desired attendees, not just new ones.
        REQUIRED: Always ask for email addresses before adding attendees. Never guess or use placeholders.
        duration_minutes alone changes length while keeping the original start time; accepts minutes or durations like "90m".
        add_meet=true attaches a video meeting link (Google Meet on google, Teams on microsoft).
        VOICE: Confirm the change briefly.
        """
        tz = _tz()
        tz_name = str(tz)

        if start is not None:
            start = _parse_calendar_datetime(start, tz).isoformat()
        if end is not None:
            end = _parse_calendar_datetime(end, tz).isoformat()
        if duration_minutes is not None:
            duration_minutes = _coerce_duration_minutes(duration_minutes)

        try:
            target = await calendar.get_event(event_id, account=account)
            target = _localize_event(target, tz)
        except Exception as exc:
            return _fail(f"Calendar event not found; no update was made. Details: {exc}")

        if expected_title or expected_start or expected_account:
            guard_error = _delete_guard_error(
                target,
                expected_title=expected_title,
                expected_start=expected_start,
                expected_account=expected_account,
                tz=tz,
            )
            if guard_error:
                return _fail(guard_error.message.replace("delete", "update").replace("Delete", "Update"))

        write_id = event_id
        if target.scope in {"occurrence", "series"} or target.series_id:
            if recurrence is not None and scope != "series":
                return _fail(
                    "Changing recurrence requires scope='series'. "
                    f"This target is scope={target.scope!r}."
                )
            if scope is None and target.scope == "occurrence":
                return _fail(
                    f"This is a recurring occurrence (scope='occurrence', "
                    f"series_id={target.series_id!r}). Pass scope='occurrence' or scope='series'."
                )
            if scope == "series":
                write_id = target.series_id or event_id

        confirmation = await calendar.update_event(
            event_id=write_id,
            account=account,
            title=title,
            start=start,
            end=end,
            duration_minutes=duration_minutes,
            description=description,
            location=location,
            attendees=attendees,
            add_meet=add_meet,
            recurrence=recurrence,
            tz_name=tz_name,
        )
        if isinstance(confirmation, CapabilityErrorDetail):
            return confirmation
        confirmation = confirmation.model_copy(update={
            "scope": scope or confirmation.scope or target.scope,
            "series_id": confirmation.series_id or target.series_id,
        })
        push_content(
            title="Calendar Event Updated",
            sections=_event_review_sections(confirmation),
            size=WidgetSize.LARGE_WIDE,
        )
        return confirmation

    @tool(inject=["calendar"])
    async def search_events(
        self,
        query: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        calendar: UnifiedCalendarClient = None,
    ) -> CalendarQueryResult | CapabilityErrorDetail:
        """
        Search calendar events by keyword across all connected calendars.
        Use for named event types like "when are my exams", "find my dentist appointment", or "any interviews this month".
        Prefer short noun queries like "exam", "dentist", or "interview"; defaults to the next 90 days.
        VOICE: Answer in prose. If none found, say so and offer to check a wider range.
        """
        tz = _tz()
        now = datetime.now(tz)

        if start_date:
            range_start = _parse_calendar_date(start_date, tz, now)
        else:
            range_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

        if end_date:
            range_end = _parse_calendar_date(end_date, tz, now) + timedelta(days=1)
        elif start_date:
            range_end = range_start + timedelta(days=90)
        else:
            range_end = range_start.replace(year=range_start.year + 1)

        result = await calendar.search_events(query, range_start.isoformat(), range_end.isoformat())
        events = _query_events(result)
        if (
            isinstance(result, CalendarQueryResult)
            and result.coverage == "partial"
            and not events
        ):
            return _fail(
                "Could not confirm that no matching events exist; coverage is partial. "
                "Retry with a narrower range or after reconnecting the failed calendar."
            )
        return CalendarQueryResult(
            events=[_localize_event(e, tz) for e in events],
            time_min=result.time_min if isinstance(result, CalendarQueryResult) else range_start.isoformat(),
            time_max=result.time_max if isinstance(result, CalendarQueryResult) else range_end.isoformat(),
            query=query,
            match_status=result.match_status if isinstance(result, CalendarQueryResult) else ("none" if not events else "multiple"),
            coverage=result.coverage if isinstance(result, CalendarQueryResult) else "complete",
            truncated=getattr(result, "truncated", False),
            failed_providers=list(getattr(result, "failed_providers", [])),
        )

    @tool(inject=["calendar"])
    async def get_next_event(
        self,
        calendar: UnifiedCalendarClient = None,
    ) -> Optional[CalendarEvent]:
        """
        Get the next upcoming timed event from right now.
        Use for "what's my next meeting?", "what's coming up?", or "what do I have next?". Looks 7 days ahead.
        VOICE: Speak naturally — "You've got X in Y minutes" or "Your next one is Z at [time]".
        """
        tz = _tz()
        now = datetime.now(tz)
        window_end = now + timedelta(days=7)

        events = await calendar.list_events(
            now.isoformat(), window_end.isoformat(), max_results=200,
        )
        timed = [e for e in _query_events(events) if not e.is_all_day]
        if not timed:
            return None
        return _localize_event(timed[0], tz)

    @tool(inject=["calendar"])
    async def find_free_time(
        self,
        start_date: str,
        end_date: Optional[str] = None,
        duration_minutes: int | str = 30,
        start_hour: int = 8,
        end_hour: int = 22,
        calendar: UnifiedCalendarClient = None,
    ) -> List[TimeSlot]:
        """
        Find free slots across a date or date range (ISO or natural date like "tomorrow").
        end_date is inclusive — set it to find a free slot "sometime this week".
        Searches across ALL connected calendars so every connection counts as busy.
        Searches between start_hour and end_hour (default 8am–10pm).
        For evening: start_hour=18. For full day: start_hour=0, end_hour=24.
        VOICE: Offer the best 1–2 slots naturally as prose. Never list raw time ranges.
        """
        tz = _tz()
        now = datetime.now(tz)
        duration_mins = _coerce_duration_minutes(duration_minutes)

        range_start = _parse_calendar_date(start_date, tz, now).replace(
            hour=max(0, min(start_hour, 23)), minute=0, second=0, microsecond=0,
        )
        search_end_date = end_date or start_date
        search_end_day = _parse_calendar_date(search_end_date, tz, now) + timedelta(days=1)

        events = await calendar.list_events(range_start.isoformat(), search_end_day.isoformat())

        busy: List[tuple] = []
        for ev in _query_events(events):
            if ev.is_all_day:
                continue
            try:
                busy.append((
                    datetime.fromisoformat(to_local_iso(ev.start, tz)),
                    datetime.fromisoformat(to_local_iso(ev.end, tz)),
                ))
            except ValueError:
                pass
        busy.sort(key=lambda x: x[0])

        needed = timedelta(minutes=duration_mins)
        slots: List[TimeSlot] = []
        # Walk each day in the range
        current_day = _parse_calendar_date(start_date, tz, now)
        while current_day < search_end_day and len(slots) < 5:
            day_start = current_day.replace(hour=max(0, min(start_hour, 23)), minute=0)
            if end_hour >= 24:
                day_end = current_day + timedelta(days=1)
            else:
                day_end = current_day.replace(hour=max(0, min(end_hour, 23)), minute=0)

            cursor = max(day_start, now) if current_day.date() == now.date() else day_start
            day_busy = [(bs, be) for bs, be in busy if be > day_start and bs < day_end]

            for bs, be in day_busy:
                gap = min(bs, day_end) - cursor
                if gap >= needed:
                    slots.append(TimeSlot(
                        start=cursor.isoformat(),
                        end=min(bs, day_end).isoformat(),
                        duration_minutes=int(gap.total_seconds() // 60),
                    ))
                cursor = max(cursor, be)
                if len(slots) >= 5:
                    break

            final_gap = day_end - cursor
            if final_gap >= needed and len(slots) < 5:
                slots.append(TimeSlot(
                    start=cursor.isoformat(),
                    end=day_end.isoformat(),
                    duration_minutes=int(final_gap.total_seconds() // 60),
                ))

            current_day += timedelta(days=1)

        return slots[:5]
