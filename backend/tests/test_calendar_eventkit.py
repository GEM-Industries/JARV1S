"""EventKit provider, Host HTTP mapping, and calendar factory."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.auth.error_handler import handle_integration_auth_error
from core.integrations.lifecycle.composio import list_integrations
from core.integrations.manager import NeedsReauth, OsPermissionNeeded
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.types import PluginMetadata
from core.time import coerce_datetime_or_none
from plugins.calendar.providers.eventkit import (
    CALENDAR_ACCESS_DENIED,
    EventKitProvider,
    _event_from_host,
    _payload_or_error,
    _window_for_event_id,
    macos_calendar_message,
    try_eventkit_provider,
)
from plugins.calendar.unified import UnifiedCalendarClient, build_unified_client


def _host_event(**overrides):
    item = {
        "id": "ext|cal-1|2026-08-29T09:00:00",
        "title": "Standup",
        "start": "2026-08-29T09:00:00",
        "end": "2026-08-29T09:30:00",
        "location": "Kitchen",
        "description": "Daily sync",
        "is_all_day": False,
        "attendees": ["Ada"],
        "calendar": "Home",
        "recurrence": "weekly",
    }
    item.update(overrides)
    return item


def test_host_json_maps_to_calendar_event():
    event = _event_from_host(_host_event())
    assert event.account == "macos"
    assert event.calendar == "Home"
    assert event.recurrence == "weekly"
    assert event.attendees == ["Ada"]
    assert event.is_all_day is False
    assert event.duration_minutes == 30


def test_host_all_day_and_unknown_recurrence():
    event = _event_from_host(_host_event(
        is_all_day=True,
        recurrence="custom",
        start="2026-08-29",
        end="2026-08-30",
    ))
    assert event.is_all_day is True
    assert event.recurrence is None
    assert event.duration_minutes is None


def test_macos_calendar_message():
    assert "this Mac" in macos_calendar_message("authorized")
    assert "System Settings" in macos_calendar_message("denied")
    assert "System Settings" in macos_calendar_message("restricted")
    assert "Allow Calendar access" in macos_calendar_message("notDetermined")
    assert "System Settings" in macos_calendar_message("notDetermined")


def test_payload_403_is_os_permission():
    with pytest.raises(OsPermissionNeeded, match="System Settings"):
        _payload_or_error(httpx.Response(403, json={"error": "permission_denied"}))


@pytest.mark.asyncio
async def test_list_events_from_host_http(monkeypatch):
    async def fake_request(method, path, *, params=None, timeout=30.0):
        assert method == "GET"
        assert path == "/events"
        return {"events": [_host_event()]}

    monkeypatch.setattr(
        "plugins.calendar.providers.eventkit._host_request",
        fake_request,
    )
    batch = await EventKitProvider().list_events(
        "2026-08-29T00:00:00", "2026-08-30T00:00:00",
    )
    assert len(batch.events) == 1
    assert batch.events[0].calendar == "Home"
    assert batch.events[0].account == "macos"


def test_window_for_event_id_covers_timezone_edges():
    time_min, time_max = _window_for_event_id("ext|cal-1|2026-08-29T23:00:00+00:00")
    instant = coerce_datetime_or_none("2026-08-29T23:00:00+00:00")
    assert coerce_datetime_or_none(time_min) == instant - timedelta(days=1)
    assert coerce_datetime_or_none(time_max) == instant + timedelta(days=1)


def test_window_for_event_id_rejects_non_mac_ids():
    with pytest.raises(RuntimeError, match="not a Mac calendar id"):
        _window_for_event_id("google-event-abc")


@pytest.mark.asyncio
async def test_search_events_matches_query_not_first_page(monkeypatch):
    def _item(title: str, hour: int) -> dict:
        return _host_event(
            id=f"ext|cal-1|2026-08-29T{hour:02d}:00:00",
            title=title,
            start=f"2026-08-29T{hour:02d}:00:00+00:00",
            end=f"2026-08-29T{hour:02d}:30:00+00:00",
        )

    clutter = [_item(f"Standup {i}", i % 24) for i in range(25)]
    dentist = _item("Dentist", 15)
    dentist["id"] = "ext|cal-1|2026-08-29T15:00:00"
    payload = clutter + [dentist]

    async def fake_request(method, path, *, params=None, timeout=30.0):
        assert params is not None
        assert "id" not in params
        return {"events": payload}

    monkeypatch.setattr(
        "plugins.calendar.providers.eventkit._host_request",
        fake_request,
    )
    batch = await EventKitProvider().search_events(
        "dentist",
        "2026-08-29T00:00:00",
        "2026-08-30T00:00:00",
        max_results=20,
    )
    assert [event.title for event in batch.events] == ["Dentist"]


@pytest.mark.asyncio
async def test_list_events_returns_earliest_first(monkeypatch):
    async def fake_request(method, path, *, params=None, timeout=30.0):
        return {
            "events": [
                _host_event(
                    id="ext|cal-1|2026-08-29T11:00:00",
                    title="Later",
                    start="2026-08-29T11:00:00+00:00",
                    end="2026-08-29T11:30:00+00:00",
                ),
                _host_event(
                    id="ext|cal-1|2026-08-29T09:00:00",
                    title="Earlier",
                    start="2026-08-29T09:00:00+00:00",
                    end="2026-08-29T09:30:00+00:00",
                ),
            ]
        }

    monkeypatch.setattr(
        "plugins.calendar.providers.eventkit._host_request",
        fake_request,
    )
    batch = await EventKitProvider().list_events(
        "2026-08-29T00:00:00", "2026-08-30T00:00:00",
    )
    assert [event.title for event in batch.events] == ["Earlier", "Later"]


@pytest.mark.asyncio
async def test_get_event_passes_expanded_window(monkeypatch):
    seen: dict[str, str] = {}

    async def fake_request(method, path, *, params=None, timeout=30.0):
        seen.update(params or {})
        return {"events": [_host_event()]}

    monkeypatch.setattr(
        "plugins.calendar.providers.eventkit._host_request",
        fake_request,
    )
    event = await EventKitProvider().get_event("ext|cal-1|2026-08-29T09:00:00")
    assert event.title == "Standup"
    assert seen["id"] == "ext|cal-1|2026-08-29T09:00:00"
    instant = coerce_datetime_or_none("2026-08-29T09:00:00")
    assert coerce_datetime_or_none(seen["time_min"]) == instant - timedelta(days=1)
    assert coerce_datetime_or_none(seen["time_max"]) == instant + timedelta(days=1)


@pytest.mark.asyncio
async def test_eventkit_writes_are_unsupported():
    provider = EventKitProvider()
    created = await provider.create_event(title="x", start="2026-08-29T09:00:00")
    updated = await provider.update_event("id", title="x")
    deleted = await provider.delete_event("id")
    assert isinstance(created, CapabilityErrorDetail)
    assert created.code == "unsupported"
    assert isinstance(updated, CapabilityErrorDetail)
    assert isinstance(deleted, CapabilityErrorDetail)


@pytest.mark.asyncio
async def test_os_permission_needed_does_not_push_oauth_widget(monkeypatch):
    pushed: list[dict] = []
    monkeypatch.setattr(
        "core.auth.error_handler.push_ui",
        lambda envelope: pushed.append(envelope.data),
    )

    result = await handle_integration_auth_error(
        "calendar",
        OsPermissionNeeded(CALENDAR_ACCESS_DENIED),
    )
    assert result is not None
    assert result.code == "permission_needed"
    assert "Calendar access" in result.message
    assert pushed == []


@pytest.mark.asyncio
async def test_eventkit_only_create_is_unsupported():
    u = UnifiedCalendarClient([EventKitProvider()])
    result = await u.create_event(title="x", start="2026-08-29T09:00:00")
    assert isinstance(result, CapabilityErrorDetail)
    assert result.code == "unsupported"


@pytest.mark.asyncio
async def test_build_unified_client_host_only(monkeypatch):
    monkeypatch.setattr("core.config.settings.HOST_CALENDAR_URL", "http://calendar.test")
    monkeypatch.setattr("core.config.settings.HOST_CALENDAR_TOKEN", "secret")

    async def ensure(_name, _scopes):
        raise NeedsReauth("google")

    monkeypatch.setattr("plugins.calendar.unified.auth_manager.ensure_scopes", ensure)
    client = await build_unified_client()
    assert [p.name for p in client._providers] == ["macos"]


@pytest.mark.asyncio
async def test_build_unified_client_no_host_no_oauth_needs_reauth(monkeypatch):
    monkeypatch.setattr("core.config.settings.HOST_CALENDAR_URL", "")
    monkeypatch.setattr("core.config.settings.HOST_CALENDAR_TOKEN", "")

    async def ensure(_name, _scopes):
        raise NeedsReauth("google")

    monkeypatch.setattr("plugins.calendar.unified.auth_manager.ensure_scopes", ensure)
    with pytest.raises(NeedsReauth):
        await build_unified_client()


@pytest.mark.asyncio
async def test_build_unified_client_google_without_eventkit(monkeypatch):
    monkeypatch.setattr("core.config.settings.HOST_CALENDAR_URL", "")
    monkeypatch.setattr("core.config.settings.HOST_CALENDAR_TOKEN", "")

    token = SimpleNamespace(access_token="g-token")

    async def ensure(name, _scopes):
        if name == "google":
            return token
        raise NeedsReauth(name)

    monkeypatch.setattr("plugins.calendar.unified.auth_manager.ensure_scopes", ensure)
    monkeypatch.setattr(
        "plugins.calendar.providers.google.create_google_client",
        lambda _t: object(),
    )
    monkeypatch.setattr(
        "plugins.calendar.providers.google.GoogleProvider",
        lambda _client: SimpleNamespace(name="google"),
    )
    client = await build_unified_client()
    assert [p.name for p in client._providers] == ["google"]


def test_try_eventkit_provider_absent_without_url(monkeypatch):
    monkeypatch.setattr("core.config.settings.HOST_CALENDAR_URL", "")
    assert try_eventkit_provider() is None


class _FakeCalendarPlugin:
    metadata = PluginMetadata(
        name="calendar",
        version="2.0.0",
        description="Calendar",
    )

    def get_tools(self):
        return ["get_events"]


@pytest.mark.asyncio
async def test_list_integrations_calendar_macos_grant_alone():
    fake_registry = MagicMock()
    fake_registry.plugins = {"calendar": _FakeCalendarPlugin()}
    fake_registry.bespoke_names = set()
    fake_registry.is_enabled.return_value = True

    async def peek_grant(_name):
        return None

    with patch("core.integrations.lifecycle.composio.registry", fake_registry), \
         patch("core.integrations.lifecycle.composio.get_composio_gateway", return_value=None), \
         patch(
             "core.integrations.manager.integrations.resolve_oauth_providers",
             return_value=["google", "microsoft"],
         ), \
         patch(
             "plugins.calendar.providers.eventkit.host_calendar_configured",
             return_value=True,
         ), \
         patch(
             "plugins.calendar.providers.eventkit.macos_calendar_status",
             AsyncMock(return_value="authorized"),
         ), \
         patch("core.auth.manager.auth_manager.peek_grant", peek_grant):
        views = await list_integrations()

    calendar = next(v for v in views if v.name == "calendar")
    assert calendar.connected is True
    assert calendar.auth_type == "macos"
    assert calendar.auth_providers[0] == "macos"
    assert calendar.connected_providers == ["macos"]


@pytest.mark.asyncio
async def test_list_integrations_calendar_macos_and_google():
    fake_registry = MagicMock()
    fake_registry.plugins = {"calendar": _FakeCalendarPlugin()}
    fake_registry.bespoke_names = set()
    fake_registry.is_enabled.return_value = True

    async def peek_grant(name):
        if name == "google":
            return object()
        return None

    with patch("core.integrations.lifecycle.composio.registry", fake_registry), \
         patch("core.integrations.lifecycle.composio.get_composio_gateway", return_value=None), \
         patch(
             "core.integrations.manager.integrations.resolve_oauth_providers",
             return_value=["google", "microsoft"],
         ), \
         patch(
             "plugins.calendar.providers.eventkit.host_calendar_configured",
             return_value=True,
         ), \
         patch(
             "plugins.calendar.providers.eventkit.macos_calendar_status",
             AsyncMock(return_value="authorized"),
         ), \
         patch("core.auth.manager.auth_manager.peek_grant", peek_grant):
        views = await list_integrations()

    calendar = next(v for v in views if v.name == "calendar")
    assert calendar.connected is True
    assert calendar.connected_providers == ["macos", "google"]
    assert calendar.auth_type == "macos"


@pytest.mark.asyncio
async def test_list_integrations_calendar_mac_only_missing():
    fake_registry = MagicMock()
    fake_registry.plugins = {"calendar": _FakeCalendarPlugin()}
    fake_registry.bespoke_names = set()
    fake_registry.is_enabled.return_value = True

    async def peek_grant(_name):
        return None

    with patch("core.integrations.lifecycle.composio.registry", fake_registry), \
         patch("core.integrations.lifecycle.composio.get_composio_gateway", return_value=None), \
         patch(
             "core.integrations.manager.integrations.resolve_oauth_providers",
             return_value=["google", "microsoft"],
         ), \
         patch(
             "plugins.calendar.providers.eventkit.host_calendar_configured",
             return_value=True,
         ), \
         patch(
             "plugins.calendar.providers.eventkit.macos_calendar_status",
             AsyncMock(return_value="denied"),
         ), \
         patch("core.auth.manager.auth_manager.peek_grant", peek_grant):
        views = await list_integrations()

    calendar = next(v for v in views if v.name == "calendar")
    assert calendar.connected is False
    assert calendar.last_error == CALENDAR_ACCESS_DENIED
    assert calendar.auth_type == "macos"
    assert calendar.connected_providers == []
