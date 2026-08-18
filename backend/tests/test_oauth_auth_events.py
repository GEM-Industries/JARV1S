"""Tests for OAuth completion event publishing."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.oauth_support import publish_oauth_changed
from api.websockets.connection import ConnectionManager
from api.websockets.types import WSMessageType
from services.events import Event, EventType


@pytest.mark.asyncio
async def test_publish_oauth_changed_emits_event_bus_message():
    published: list[dict] = []

    async def _capture(event):
        published.append({"type": event.type.value, "data": event.data})

    with patch("services.events.event_bus.publish", new=AsyncMock(side_effect=_capture)):
        await publish_oauth_changed(app="google", success=True, loaded=True, kind="bespoke")

    assert len(published) == 1
    assert published[0]["type"] == "auth.oauth.changed"
    assert published[0]["data"]["app"] == "google"
    assert published[0]["data"]["success"] is True
    assert published[0]["data"]["loaded"] is True
    assert published[0]["data"]["kind"] == "bespoke"
    assert published[0]["data"]["owner_id"]


@pytest.mark.asyncio
async def test_connection_manager_forwards_oauth_events_to_owner_sessions():
    manager = ConnectionManager()
    sent: list[tuple[str, object]] = []

    class _Session:
        owner_id = "owner-1"
        connection_id = "conn-1"

    async def _capture(connection_id, response):
        sent.append((connection_id, response))

    manager.sessions["conn-1"] = _Session()  # type: ignore[assignment]
    manager.send_message = AsyncMock(side_effect=_capture)  # type: ignore[method-assign]

    await manager._handle_auth_oauth_changed(
        Event(
            type=EventType.AUTH_OAUTH_CHANGED,
            source="test",
            data={
                "owner_id": "owner-1",
                "app": "google",
                "success": True,
                "loaded": True,
                "kind": "bespoke",
            },
        )
    )

    assert len(sent) == 1
    assert sent[0][0] == "conn-1"
    assert sent[0][1].type == WSMessageType.AUTH_OAUTH_CHANGED
    assert sent[0][1].data["app"] == "google"
    assert sent[0][1].data["success"] is True


def test_resolve_oauth_provider_for_providerless_calendar():
    from core.integrations.manager import IntegrationManager

    manager = IntegrationManager()
    manager.register("calendar", lambda _c: object())
    manager.register("weather", lambda _c: object())
    manager.register_aux_provider_scopes(
        "google",
        ["calendar.readonly"],
        integration_name="calendar",
    )
    manager.register_aux_provider_scopes(
        "microsoft",
        ["Calendars.Read"],
        integration_name="calendar",
    )

    assert manager.resolve_oauth_provider("calendar") == "google"
    assert manager.resolve_oauth_providers("calendar") == ["google", "microsoft"]
    assert manager.resolve_oauth_provider("weather") is None
    assert manager.resolve_oauth_providers("weather") == []
