# Multi-Provider Calendar

**Status:** Implemented (Phase 9.5)
**Date:** 2026-03-01

---

## Problem

The calendar plugin was originally hardcoded to Google Calendar. Real life spans multiple providers — Outlook for work, Google for personal, shared family calendars, etc. "Am I free Thursday?" must query all of them or the answer is wrong.

---

## Design Principle

The plugin layer should never know which providers exist. `@tool(inject=["calendar"])` now gives the plugin a `UnifiedCalendarClient` that wraps N providers, while provider-specific parsing and API calls live behind the calendar provider boundary.

```
CalendarPlugin                   client.py
┌──────────────┐                 ┌──────────────────────────┐
│ get_events() │                 │ UnifiedCalendarClient     │
│ create_event │──@tool inject──▶│   .list_events()         │
│ find_free()  │                 │   .create_event()        │
│ delete_event │                 │   .delete_event()        │
└──────────────┘                 │   .get_event()           │
                                 │       │                   │
                                 │   ┌───┴────────────────┐  │
                                 │   │ GoogleProvider      │  │
                                 │   │ OutlookProvider     │  │
                                 │   │ (future: iCloud)    │  │
                                 │   └────────────────────┘  │
                                 └──────────────────────────┘
```

---

## Architecture

### CalendarProvider Protocol

A minimal async interface each backend implements:

```python
class CalendarProvider(Protocol):
    name: str  # "google" | "microsoft"

    async def list_events(self, time_min: str, time_max: str, max_results: int = 50) -> List[CalendarEvent]
    async def get_event(self, event_id: str) -> CalendarEvent
    async def create_event(...) -> EventConfirmation
    async def update_event(...) -> EventConfirmation
    async def search_events(...) -> List[CalendarEvent]
    async def delete_event(self, event_id: str) -> str
    async def refresh(self) -> None
```

Providers return already-parsed `CalendarEvent` / `EventConfirmation` objects. Raw Google or Microsoft payloads do not cross the provider boundary.

### UnifiedCalendarClient

Wraps one or more providers. Constructed by the factory based on which credentials are configured.

**Reads** (`list_events`, `search_events`) — fan out to all providers in parallel (`asyncio.gather`), merge, deduplicate by `(account, id)`, sort by start time.

**Writes** (`create_event`, `update_event`, `delete_event`) — route to a single provider by account label. If only one provider is connected, the account hint can be omitted.

**Refresh** — each provider manages its own token lifecycle. The unified client's refresh hook calls `provider.refresh()` on each.

### Fail-Safe Registration

```python
# build_unified_client()
providers = []
if auth_manager.ensure_scopes("google", GOOGLE_SCOPES):
    providers.append(GoogleProvider(...))
if auth_manager.ensure_scopes("microsoft", MICROSOFT_SCOPES):
    providers.append(OutlookProvider(...))

if not providers:
    raise NeedsReauth("calendar")

return UnifiedCalendarClient(providers, account_map=settings.ACCOUNT_PROVIDERS)
```

No usable provider credentials → calendar raises `NeedsReauth("calendar")`. One connected provider is enough for the plugin to work.

---

## Provider Implementations

### GoogleProvider

Google Calendar API v3 provider. Parses Google payloads into `CalendarEvent` / `EventConfirmation`, stamps the configured account label, and owns Google-specific calendar discovery / Meet behavior.

### OutlookProvider

Microsoft Graph API (`https://graph.microsoft.com/v1.0/me`). Same async `httpx` transport pattern — no MS SDK needed.

- **Auth:** Microsoft OAuth through the shared `AuthManager` / Integration Gate.
- **Endpoints:** `/me/calendars` (list), `/me/calendars/{id}/events`, plus direct event CRUD routes.
- **Scope:** `Calendars.ReadWrite` (single scope covers everything including calendar discovery).

### Future Providers

iCloud (via CalDAV + `caldav` library), CalDAV self-hosted, etc. Each is a new `CalendarProvider` implementation — no changes to the plugin or unified client.

---

## File Changes

| File | Change |
|------|--------|
| `plugins/calendar/client.py` | Replace with: `CalendarProvider` protocol, `GoogleProvider`, `UnifiedCalendarClient`, factory. Existing Google logic refactored, not rewritten. |
| `plugins/calendar/outlook.py` | **New.** `OutlookProvider` implementation + token helpers. |
| `plugins/calendar/__init__.py` | Minimal — `_fetch_events` calls `client.list_events()` instead of raw httpx. `_get_calendar_ids` removed (providers handle their own calendar discovery). `register_integrations` updated to use new factory. |
| `cli/setup_outlook.py` | **New.** Guided OAuth setup for Microsoft Graph. Same pattern as `setup_calendar.py`. |
| `cli/setup_calendar.py` | No change (Google setup stays as-is). |
| `.env` | Add optional `OUTLOOK_CALENDAR_CREDENTIALS=.credentials/outlook_token.json`. |
| `Taskfile.yml` | Add `setup:outlook` task. |

---

## Plugin Impact

The plugin now injects `UnifiedCalendarClient`, exposes account-aware read/write tools, and returns events stamped with `account` so follow-up mutations can route back to the provider that produced the event.

Provider-specific parsing and API calls live in `providers/google.py` and `providers/outlook.py`; the plugin layer deals in normalized calendar models.

---

## Setup UX

Calendar uses the shared in-UI OAuth flow. Google and Microsoft provider scopes are aggregated by the Integration Gate; when both are connected, reads fan out across both providers. A single connected provider still works.

---

## What This Does NOT Do

- **No provider selection in the LLM.** The agent never says "which calendar?" — reads merge silently, writes go to the default provider.
- **No CalDAV yet.** Third provider when needed — the protocol makes it trivial.
- **No sync engine.** Each query hits live APIs. No local database, no background sync, no conflict resolution between providers.
- **No new plugin tools.** The existing 5 tools cover the full user-facing surface.
