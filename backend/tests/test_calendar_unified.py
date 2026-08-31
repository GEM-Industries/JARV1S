"""Unit tests for the Multi-Provider Calendar (UnifiedCalendarClient).

Covers:
- Fan-out read merging across providers.
- Dedup by (account, id) — same-id across providers stays distinct.
- Account-routed writes.
- Unknown account label raises.
- Single-provider omit-account fallback.
- One provider failing does not fail the whole read.
"""

from unittest.mock import AsyncMock

import pytest

from core.integrations.manager import NeedsReauth
from plugins.calendar.models import CalendarEvent, EventConfirmation
from plugins.calendar.providers.base import ProviderEventBatch
from plugins.calendar.unified import UnifiedCalendarClient


class _FakeProvider:
    """Minimal CalendarProvider stand-in for fan-out + routing tests."""

    def __init__(
        self,
        name: str,
        events=None,
        fail_list: bool = False,
        incomplete: bool = False,
    ):
        self.name = name
        self._events = events or []
        self._fail_list = fail_list
        self._incomplete = incomplete
        self.list_calls = []
        self.search_queries = []
        self.create_event = AsyncMock()
        self.update_event = AsyncMock()
        self.delete_event = AsyncMock()
        self.get_event = AsyncMock()
        self.refresh = AsyncMock()

    async def list_events(self, time_min, time_max, max_results=50):
        self.list_calls.append(max_results)
        if self._fail_list:
            raise RuntimeError(f"{self.name} list failed")
        stamped = [
            event.model_copy(update={"account": self.name})
            for event in self._events
        ]
        return ProviderEventBatch(
            events=stamped[:max_results],
            incomplete=self._incomplete,
        )

    async def search_events(self, query, time_min, time_max, max_results=20):
        self.search_queries.append(query)
        batch = await self.list_events(time_min, time_max, max_results)
        needle = query.casefold()
        return ProviderEventBatch(
            events=[
                event
                for event in batch.events
                if needle in event.title.casefold()
            ]
        )


def _event(id_: str, start: str, title: str = "evt") -> CalendarEvent:
    return CalendarEvent(id=id_, title=title, start=start, end=start)


# --- resolve_account ------------------------------------------------------


class TestResolveAccount:
    def test_single_provider_none_account_returns_that_provider(self):
        p = _FakeProvider("google")
        u = UnifiedCalendarClient([p])
        assert u.resolve_account(None) is p

    def test_multi_provider_none_account_raises(self):
        p1 = _FakeProvider("google")
        p2 = _FakeProvider("microsoft")
        u = UnifiedCalendarClient([p1, p2])
        with pytest.raises(ValueError, match="google"):
            u.resolve_account(None)

    def test_known_name_routes(self):
        p1 = _FakeProvider("google")
        p2 = _FakeProvider("microsoft")
        u = UnifiedCalendarClient([p1, p2])
        assert u.resolve_account("google") is p1
        assert u.resolve_account("microsoft") is p2

    def test_unknown_name_raises(self):
        p1 = _FakeProvider("google")
        u = UnifiedCalendarClient([p1])
        with pytest.raises(ValueError, match="Unknown account 'family'"):
            u.resolve_account("family")

    def test_name_for_disconnected_provider_raises_value_error(self):
        p1 = _FakeProvider("google")
        u = UnifiedCalendarClient([p1])
        with pytest.raises(ValueError, match="microsoft"):
            u.resolve_account("microsoft")

    def test_eventkit_plus_google_none_account_writes_google(self):
        macos = _FakeProvider("macos")
        google = _FakeProvider("google")
        u = UnifiedCalendarClient([macos, google])
        assert u.resolve_account(None) is google
        assert u.resolve_account("macos") is macos

    def test_eventkit_google_microsoft_none_account_raises(self):
        u = UnifiedCalendarClient([
            _FakeProvider("macos"),
            _FakeProvider("google"),
            _FakeProvider("microsoft"),
        ])
        with pytest.raises(ValueError, match="google"):
            u.resolve_account(None)

    def test_empty_providers_raises_needs_reauth(self):
        with pytest.raises(NeedsReauth):
            UnifiedCalendarClient([])


# --- list_events ----------------------------------------------------------


class TestListEvents:
    @pytest.mark.asyncio
    async def test_fan_out_merges_and_sorts(self):
        p1 = _FakeProvider(
            "google",
            events=[_event("g1", "2026-05-01T10:00:00"), _event("g2", "2026-05-01T08:00:00")],
        )
        p2 = _FakeProvider(
            "microsoft",
            events=[_event("m1", "2026-05-01T09:00:00")],
        )
        u = UnifiedCalendarClient([p1, p2])

        result = await u.list_events("2026-05-01T00:00:00", "2026-05-02T00:00:00")

        assert [e.id for e in result.events] == ["g2", "m1", "g1"]
        assert result.coverage == "complete"
        assert result.match_status == "multiple"
        # Providers stamped account on their own events.
        assert {e.account for e in result.events} == {"google", "microsoft"}

    @pytest.mark.asyncio
    async def test_dedup_uses_account_plus_id(self):
        # Same id on different providers must stay distinct.
        p1 = _FakeProvider("google", events=[_event("shared", "2026-05-01T10:00:00")])
        p2 = _FakeProvider("microsoft", events=[_event("shared", "2026-05-01T10:00:00")])
        u = UnifiedCalendarClient([p1, p2])

        result = await u.list_events("2026-05-01T00:00:00", "2026-05-02T00:00:00")
        assert len(result.events) == 2
        assert {e.account for e in result.events} == {"google", "microsoft"}

    @pytest.mark.asyncio
    async def test_one_provider_failure_does_not_fail_read(self):
        p1 = _FakeProvider("google", events=[_event("g1", "2026-05-01T10:00:00")])
        p2 = _FakeProvider("microsoft", fail_list=True)
        u = UnifiedCalendarClient([p1, p2])

        result = await u.list_events("2026-05-01T00:00:00", "2026-05-02T00:00:00")
        assert [e.id for e in result.events] == ["g1"]
        assert result.coverage == "partial"
        assert result.failed_providers == ["microsoft"]

    @pytest.mark.asyncio
    async def test_provider_internal_partial_read_is_visible(self):
        provider = _FakeProvider(
            "google",
            events=[_event("g1", "2026-05-01T10:00:00")],
            incomplete=True,
        )

        result = await UnifiedCalendarClient([provider]).list_events(
            "2026-05-01T00:00:00",
            "2026-05-02T00:00:00",
        )

        assert [event.id for event in result.events] == ["g1"]
        assert result.coverage == "partial"
        assert result.failed_providers == ["google"]

    @pytest.mark.asyncio
    async def test_max_results_cap(self):
        p1 = _FakeProvider(
            "google",
            events=[_event(f"g{i}", f"2026-05-01T{i:02d}:00:00") for i in range(10)],
        )
        u = UnifiedCalendarClient([p1])
        result = await u.list_events(
            "2026-05-01T00:00:00", "2026-05-02T00:00:00", max_results=3,
        )
        assert len(result.events) == 3
        assert result.truncated is True
        assert result.coverage == "partial"

    @pytest.mark.asyncio
    async def test_empty_results_do_not_retry_with_expanded_limit(self):
        p1 = _FakeProvider("google", events=[])
        u = UnifiedCalendarClient([p1])

        result = await u.list_events(
            "2026-05-01T00:00:00", "2026-05-02T00:00:00",
        )

        assert result.events == []
        assert result.match_status == "none"
        assert result.coverage == "complete"
        assert p1.list_calls == [50]

    @pytest.mark.asyncio
    async def test_all_provider_failures_raise_without_expanded_retry(self):
        p1 = _FakeProvider("google", fail_list=True)
        u = UnifiedCalendarClient([p1])

        with pytest.raises(RuntimeError):
            await u.list_events(
                "2026-05-01T00:00:00", "2026-05-02T00:00:00",
            )

        assert p1.list_calls == [50]


class TestSearchEvents:
    @pytest.mark.asyncio
    async def test_fuzzy_fallback_recovers_small_spelling_error(self):
        provider = _FakeProvider(
            "google",
            events=[_event("dentist", "2026-05-01T10:00:00", title="Dentist appointment")],
        )
        client = UnifiedCalendarClient([provider])

        result = await client.search_events(
            "dentst",
            "2026-05-01T00:00:00",
            "2026-05-02T00:00:00",
        )

        assert [event.id for event in result.events] == ["dentist"]
        assert result.match_status == "single"
        assert result.coverage == "complete"

    @pytest.mark.asyncio
    async def test_tokenized_provider_search_and_local_filter(self):
        provider = _FakeProvider(
            "google",
            events=[
                _event("mum", "2026-05-10", title="Mums birthday"),
                _event("other", "2026-05-11", title="Team offsite"),
            ],
        )
        client = UnifiedCalendarClient([provider])

        result = await client.search_events(
            "Mum birthday",
            "2026-01-01T00:00:00",
            "2027-01-01T00:00:00",
        )

        assert [event.id for event in result.events] == ["mum"]
        assert {"Mum birthday", "mum", "birthday"}.issubset(set(provider.search_queries))
        assert result.match_status == "single"
        assert result.coverage == "complete"

    @pytest.mark.asyncio
    async def test_scaled_fallback_preserves_partial_coverage(self):
        events = [
            _event("target", "2026-04-29", title="Charlie Birthday"),
            *[
                _event(f"e{i}", f"2026-{(i % 12) + 1:02d}-15", title=f"Noise {i}")
                for i in range(600)
            ],
        ]
        provider = _FakeProvider("google", events=events)
        # Force provider search to miss so inventory fallback runs.
        provider.search_events = AsyncMock(return_value=ProviderEventBatch(events=[]))
        client = UnifiedCalendarClient([provider])

        result = await client.search_events(
            "Charlie birthday",
            "2026-01-01T00:00:00",
            "2027-01-01T00:00:00",
            max_results=20,
        )

        assert provider.list_calls[-1] >= 365
        assert [event.id for event in result.events] == ["target"]
        assert result.truncated is True
        assert result.coverage == "partial"


# --- write routing --------------------------------------------------------


class TestWriteRouting:
    @pytest.mark.asyncio
    async def test_create_event_routes_by_account(self):
        p1 = _FakeProvider("google")
        p2 = _FakeProvider("microsoft")
        p1.create_event.return_value = EventConfirmation(
            id="p1", title="t", start="s", end="e", account="google",
        )
        p2.create_event.return_value = EventConfirmation(
            id="p2", title="t", start="s", end="e", account="microsoft",
        )
        u = UnifiedCalendarClient([p1, p2])

        result = await u.create_event(title="t", start="2026-05-01T10:00:00", account="microsoft")
        p2.create_event.assert_awaited_once()
        p1.create_event.assert_not_awaited()
        assert result.id == "p2"

    @pytest.mark.asyncio
    async def test_update_event_routes_by_account(self):
        p1 = _FakeProvider("google")
        p2 = _FakeProvider("microsoft")
        p2.update_event.return_value = EventConfirmation(
            id="e1", title="t", start="s", end="e", account="microsoft",
        )
        u = UnifiedCalendarClient([p1, p2])

        await u.update_event(event_id="e1", account="microsoft", title="new")
        p2.update_event.assert_awaited_once()
        p1.update_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delete_event_routes_by_account(self):
        p1 = _FakeProvider("google")
        p2 = _FakeProvider("microsoft")
        p1.delete_event.return_value = "Deleted."
        u = UnifiedCalendarClient([p1, p2])

        await u.delete_event(event_id="x", account="google")
        p1.delete_event.assert_awaited_once_with("x")
        p2.delete_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_event_unknown_account_raises(self):
        p1 = _FakeProvider("google")
        u = UnifiedCalendarClient([p1])
        with pytest.raises(ValueError):
            await u.create_event(title="t", start="2026-05-01T10:00:00", account="family")

    @pytest.mark.asyncio
    async def test_single_provider_omit_account_works(self):
        p1 = _FakeProvider("google")
        p1.create_event.return_value = EventConfirmation(
            id="p1", title="t", start="s", end="e", account="google",
        )
        u = UnifiedCalendarClient([p1])

        await u.create_event(title="t", start="2026-05-01T10:00:00")
        p1.create_event.assert_awaited_once()


# --- get_event ------------------------------------------------------------


class TestGetEvent:
    @pytest.mark.asyncio
    async def test_get_event_with_account_routes_direct(self):
        p1 = _FakeProvider("google")
        p2 = _FakeProvider("microsoft")
        p2.get_event.return_value = _event("evt", "2026-05-01T10:00:00").model_copy(
            update={"account": "microsoft"}
        )
        u = UnifiedCalendarClient([p1, p2])

        result = await u.get_event("evt", account="microsoft")
        p2.get_event.assert_awaited_once_with("evt")
        p1.get_event.assert_not_awaited()
        assert result.account == "microsoft"

    @pytest.mark.asyncio
    async def test_get_event_without_account_tries_each_provider(self):
        p1 = _FakeProvider("google")
        p2 = _FakeProvider("microsoft")
        p1.get_event.side_effect = RuntimeError("not found")
        p2.get_event.return_value = _event("evt", "2026-05-01T10:00:00").model_copy(
            update={"account": "microsoft"}
        )
        u = UnifiedCalendarClient([p1, p2])

        result = await u.get_event("evt")
        assert result.account == "microsoft"
        p1.get_event.assert_awaited_once()
        p2.get_event.assert_awaited_once()


# --- refresh --------------------------------------------------------------


class TestRefresh:
    @pytest.mark.asyncio
    async def test_refresh_calls_each_provider(self):
        p1 = _FakeProvider("google")
        p2 = _FakeProvider("microsoft")
        u = UnifiedCalendarClient([p1, p2])

        await u.refresh()
        p1.refresh.assert_awaited_once()
        p2.refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresh_failure_propagates(self):
        p1 = _FakeProvider("google")
        p1.refresh.side_effect = NeedsReauth("google")
        u = UnifiedCalendarClient([p1])

        with pytest.raises(NeedsReauth):
            await u.refresh()
