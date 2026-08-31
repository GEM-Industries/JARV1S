"""CalendarWatcher — polls the UnifiedCalendarClient via the Integration Gate."""

import logging
from datetime import datetime, timedelta, timezone

from core.plugins.read_evidence import ReadCoverage

logger = logging.getLogger(__name__)


class CalendarWatcher:
    """
    Polls every connected calendar provider (this Mac, Google, Microsoft, …)
    for upcoming events in a 24h window. Reads flow through the
    UnifiedCalendarClient so multi-calendar discovery, deduplication, provider
    fan-out, and attendee filtering stay in sync with the LLM-facing calendar tool.

    NOTE: This watcher uses the single global calendar integration client.
    In a multi-user deployment, watchers would need to be per-user instances.
    Currently scoped to Phase 1 (single user).
    """

    source = "calendar"
    trigger_mode = "anticipated"
    trigger_events = [
        {
            "event": "starting",
            "description": (
                "Triggers relative to a calendar event's start time. "
                "Use trigger.offset (negative = before, 0 = at start). "
                "Built-in — no external connection required."
            ),
        },
    ]
    condition_fields = [
        {"field": "title", "type": "string"},
        {"field": "location", "type": "string"},
        {"field": "description", "type": "string"},
        {"field": "is_all_day", "type": "boolean", "hint": "equals 'true' or 'false'"},
        {"field": "attendees", "type": "string", "hint": "use contains to match a specific email"},
        {"field": "attendee_count", "type": "number", "hint": "excludes self"},
        {"field": "duration_minutes", "type": "number", "hint": "timed events only, null for all-day"},
        {"field": "meet_link", "type": "string"},
        {"field": "account", "type": "string", "hint": "'google', 'microsoft', or 'macos' — which connection it came from"},
    ]

    async def poll(self) -> list[dict]:
        """Return upcoming events across every connected calendar in the next 24 hours."""
        try:
            from core.integrations import integrations
            calendar = await integrations.get("calendar")
        except Exception:
            logger.debug("Calendar integration not configured, skipping poll.")
            return []

        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(hours=24)).isoformat()

        result = await calendar.list_events(time_min, time_max)
        if result.coverage != ReadCoverage.COMPLETE:
            failed = ", ".join(result.failed_providers) or "truncated coverage"
            raise RuntimeError(f"Calendar poll coverage was partial: {failed}")
        return [event.model_dump() for event in result.events]
