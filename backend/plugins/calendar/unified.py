"""
UnifiedCalendarClient — one facade across N CalendarProvider implementations.

- Reads fan out across every configured provider with asyncio.gather.
- Writes route to a single provider chosen by connection name
  (`google` | `microsoft` | `macos`).
- Event objects returned from reads are stamped with `provider.name` so the
  LLM can pass that value straight back into update_event / delete_event.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional

from core.auth.exceptions import ScopeGapError
from core.auth.manager import auth_manager
from core.integrations.manager import NeedsReauth

from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.read_evidence import ReadCoverage, match_status_from_count
from plugins.calendar.models import (
    CalendarEvent,
    CalendarQueryResult,
    CalendarRecurrence,
    EventConfirmation,
)
from plugins.calendar.providers.base import CalendarProvider, ProviderEventBatch

logger = logging.getLogger(__name__)

_MAX_PROVIDER_SEARCH_TERMS = 3


def _searchable_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for raw in text.lower().replace("/", " ").replace("-", " ").split():
        term = "".join(ch for ch in raw if ch.isalnum())
        if not term:
            continue
        terms.add(term)
        if len(term) >= 3 and term.endswith("s"):
            terms.add(term[:-1])
        elif len(term) >= 3:
            terms.add(term + "s")
    return terms


def _provider_search_queries(query: str) -> list[str]:
    """Bounded provider queries: full phrase plus the strongest individual tokens."""
    cleaned = " ".join(query.split()).strip()
    queries: list[str] = []
    if cleaned:
        queries.append(cleaned)

    seen = {cleaned.casefold()} if cleaned else set()
    for raw in cleaned.lower().replace("/", " ").replace("-", " ").split():
        term = "".join(ch for ch in raw if ch.isalnum())
        if len(term) < 3 or term in seen:
            continue
        queries.append(term)
        seen.add(term)
        if len(queries) > _MAX_PROVIDER_SEARCH_TERMS:
            break
    return queries or [query]


def _matches_query(event: CalendarEvent, query: str) -> bool:
    haystack = " ".join(
        part for part in (event.title, event.description, event.location) if part
    )
    query_terms = {
        "".join(ch for ch in raw if ch.isalnum())
        for raw in query.lower().replace("/", " ").replace("-", " ").split()
    }
    query_terms.discard("")
    event_terms = _searchable_terms(haystack)
    if not query_terms:
        return False
    return all(
        term in event_terms
        or (
            len(term) >= 3 and term.endswith("s") and term[:-1] in event_terms
        )
        or (
            len(term) >= 3 and f"{term}s" in event_terms
        )
        or (
            len(term) >= 4
            and any(
                SequenceMatcher(None, term, candidate).ratio() >= 0.8
                for candidate in event_terms
                if len(candidate) >= 4
            )
        )
        for term in query_terms
    )


def _window_scan_limit(time_min: str, time_max: str, max_results: int) -> int:
    try:
        start = datetime.fromisoformat(time_min)
        end = datetime.fromisoformat(time_max)
        window_days = max(1, (end - start).days)
    except ValueError:
        window_days = 1
    return min(500, max(max_results * 5, window_days * 50, 100))


def _query_result(
    events: List[CalendarEvent],
    *,
    time_min: str,
    time_max: str,
    query: str | None,
    truncated: bool,
    failed_providers: List[str],
) -> CalendarQueryResult:
    visible = events
    return CalendarQueryResult(
        events=visible,
        time_min=time_min,
        time_max=time_max,
        query=query,
        match_status=match_status_from_count(len(visible)),
        coverage=(
            ReadCoverage.PARTIAL
            if truncated or failed_providers
            else ReadCoverage.COMPLETE
        ),
        truncated=truncated,
        failed_providers=failed_providers,
    )


class UnifiedCalendarClient:
    """Multi-provider facade the CalendarPlugin tools inject against."""

    def __init__(self, providers: List[CalendarProvider]):
        if not providers:
            raise NeedsReauth("calendar")
        self._providers: List[CalendarProvider] = providers
        self._by_name: Dict[str, CalendarProvider] = {p.name: p for p in providers}

    def get_provider(self, provider_name: str) -> Optional[CalendarProvider]:
        """Look up a provider by its canonical name (e.g. "google", "microsoft")."""
        return self._by_name.get(provider_name)

    def resolve_account(self, account: Optional[str]) -> CalendarProvider:
        """Map a connection name (`google` | `microsoft` | `macos`) to its provider.

        - Named: that loaded provider, else ValueError listing connected names.
        - None + one provider: that provider.
        - None + several + exactly one non-macos: that OAuth writer (EventKit is
          read-only in V0).
        - Else: ValueError listing loaded names.
        """
        names = [p.name for p in self._providers]
        listed = ", ".join(names) or "none"
        if account is not None:
            provider = self._by_name.get(account)
            if provider is None:
                raise ValueError(
                    f"Unknown account '{account}'. Connected: {listed}."
                )
            return provider

        if len(self._providers) == 1:
            return self._providers[0]

        writers = [p for p in self._providers if p.name != "macos"]
        if len(writers) == 1:
            return writers[0]
        raise ValueError(
            "Multiple calendar connections are active; pass account as "
            + " or ".join(repr(n) for n in names)
            + "."
        )

    async def list_events(
        self,
        time_min: str,
        time_max: str,
        max_results: int = 50,
    ) -> CalendarQueryResult:
        results = await asyncio.gather(
            *[p.list_events(time_min, time_max, max_results) for p in self._providers],
            return_exceptions=True,
        )

        merged: List[CalendarEvent] = []
        errors: List[Exception] = []
        failed_providers: List[str] = []
        provider_truncated = False
        for p, r in zip(self._providers, results):
            if isinstance(r, ProviderEventBatch):
                merged.extend(r.events)
                provider_truncated = (
                    provider_truncated or len(r.events) >= max_results
                )
                if r.incomplete:
                    failed_providers.append(p.name)
            elif isinstance(r, Exception):
                errors.append(r)
                failed_providers.append(p.name)
                logger.warning("Provider '%s' list_events failed: %s", p.name, r)

        if errors and len(errors) == len(self._providers):
            raise errors[0]

        seen: set[tuple[str, str]] = set()
        unique: List[CalendarEvent] = []
        for ev in merged:
            key = (ev.account or "", ev.id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(ev)

        unique.sort(key=lambda e: e.start)
        truncated = provider_truncated or len(unique) > max_results
        return _query_result(
            unique[:max_results],
            time_min=time_min,
            time_max=time_max,
            query=None,
            truncated=truncated,
            failed_providers=failed_providers,
        )

    async def get_event(
        self, event_id: str, account: Optional[str] = None,
    ) -> CalendarEvent:
        if account is not None:
            return await self.resolve_account(account).get_event(event_id)

        last_exc: Exception | None = None
        for p in self._providers:
            try:
                return await p.get_event(event_id)
            except Exception as e:
                last_exc = e
                continue
        raise last_exc or RuntimeError(f"Event {event_id!r} not found on any provider")

    async def search_events(
        self,
        query: str,
        time_min: str,
        time_max: str,
        max_results: int = 20,
    ) -> CalendarQueryResult:
        """Fan out keyword searches, then keep only locally matching candidates."""
        search_queries = _provider_search_queries(query)
        provider_jobs = [
            (provider, search_query)
            for provider in self._providers
            for search_query in search_queries
        ]
        results = await asyncio.gather(
            *[
                provider.search_events(search_query, time_min, time_max, max_results)
                for provider, search_query in provider_jobs
            ],
            return_exceptions=True,
        )

        merged: List[CalendarEvent] = []
        errors_by_provider: Dict[str, Exception] = {}
        failed_providers: List[str] = []
        provider_truncated = False
        providers_with_success: set[str] = set()
        for (provider, _search_query), result in zip(provider_jobs, results):
            if isinstance(result, ProviderEventBatch):
                providers_with_success.add(provider.name)
                merged.extend(result.events)
                provider_truncated = (
                    provider_truncated or len(result.events) >= max_results
                )
                if result.incomplete and provider.name not in failed_providers:
                    failed_providers.append(provider.name)
            elif isinstance(result, Exception):
                errors_by_provider[provider.name] = result

        for provider in self._providers:
            if provider.name in providers_with_success:
                continue
            error = errors_by_provider.get(provider.name)
            if error is None:
                continue
            failed_providers.append(provider.name)
            logger.warning("Provider '%s' search_events failed: %s", provider.name, error)

        if errors_by_provider and not providers_with_success:
            raise next(iter(errors_by_provider.values()))

        seen: set[tuple[str, str]] = set()
        unique: List[CalendarEvent] = []
        for ev in merged:
            key = (ev.account or "", ev.id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(ev)

        unique = [ev for ev in unique if _matches_query(ev, query)]
        unique.sort(key=lambda e: e.start)
        if not unique:
            fallback_limit = _window_scan_limit(time_min, time_max, max_results)
            fallback = await self.list_events(time_min, time_max, max_results=fallback_limit)
            unique = [ev for ev in fallback.events if _matches_query(ev, query)]
            failed_providers = fallback.failed_providers
            provider_truncated = fallback.truncated

        truncated = provider_truncated or len(unique) > max_results
        return _query_result(
            unique[:max_results],
            time_min=time_min,
            time_max=time_max,
            query=query,
            truncated=truncated,
            failed_providers=failed_providers,
        )

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
        account: Optional[str] = None,
    ) -> EventConfirmation | CapabilityErrorDetail:
        provider = self.resolve_account(account)
        return await provider.create_event(
            title=title,
            start=start,
            duration_minutes=duration_minutes,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
            add_meet=add_meet,
            all_day=all_day,
            recurrence=recurrence,
            tz_name=tz_name,
        )

    async def update_event(
        self,
        event_id: str,
        account: Optional[str] = None,
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
        provider = self.resolve_account(account)
        return await provider.update_event(
            event_id=event_id,
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

    async def delete_event(
        self, event_id: str, account: Optional[str] = None,
    ) -> str | CapabilityErrorDetail:
        provider = self.resolve_account(account)
        return await provider.delete_event(event_id)

    async def refresh(self) -> None:
        await asyncio.gather(*[p.refresh() for p in self._providers])


async def _try_provider(
    provider_name: str,
    scopes: List[str],
    build: Callable[[str], CalendarProvider],
) -> Optional[CalendarProvider]:
    """Ensure scopes + build one OAuth provider. Returns None on missing/bad credentials."""
    try:
        token = await auth_manager.ensure_scopes(provider_name, scopes)
        return build(token.access_token)
    except (NeedsReauth, ScopeGapError) as e:
        logger.info("%s Calendar not available: %s", provider_name.title(), e)
    except Exception as e:
        logger.warning("%s Calendar init failed: %s", provider_name.title(), e)
    return None


async def build_unified_client() -> UnifiedCalendarClient:
    """Build UnifiedCalendarClient from connected OAuth providers plus Host EventKit.

    Fail-safe: zero connected providers raises NeedsReauth("calendar").
    EventKit loads whenever the Host URL is set, even if permission is notDetermined.
    """
    from plugins.calendar.providers.eventkit import try_eventkit_provider
    from plugins.calendar.providers.google import (
        GOOGLE_CALENDAR_SCOPES,
        GoogleProvider,
        create_google_client,
    )
    from plugins.calendar.providers.outlook import (
        OUTLOOK_CALENDAR_SCOPES,
        OutlookProvider,
        create_outlook_client,
    )

    google, microsoft = await asyncio.gather(
        _try_provider(
            "google",
            GOOGLE_CALENDAR_SCOPES,
            lambda t: GoogleProvider(create_google_client(t)),
        ),
        _try_provider(
            "microsoft",
            OUTLOOK_CALENDAR_SCOPES,
            lambda t: OutlookProvider(create_outlook_client(t)),
        ),
    )
    providers = [p for p in (google, microsoft, try_eventkit_provider()) if p is not None]

    if not providers:
        raise NeedsReauth("calendar")

    return UnifiedCalendarClient(providers)


async def create_calendar_client(_config: Dict[str, Any]) -> UnifiedCalendarClient:
    """IntegrationManager factory. `_config` is unused; scope validation happens
    per-provider inside build_unified_client via AuthManager.ensure_scopes."""
    return await build_unified_client()


async def refresh_calendar_client(
    client: UnifiedCalendarClient, _config: Dict[str, Any]
) -> None:
    """IntegrationManager refresh hook: refresh each provider's OAuth token."""
    await client.refresh()
