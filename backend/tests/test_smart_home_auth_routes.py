"""Additional smart-home route coverage for discovery and authorization."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.responses import HTMLResponse

import api.routes.smart_home as smart_home_routes
from plugins.smart_home.auth_flow import PendingHaAuthFlow, clear_pending_for_tests


@pytest.fixture(autouse=True)
def _clear_ha_auth() -> None:
    clear_pending_for_tests()


@pytest.mark.asyncio
async def test_discover_route_returns_found_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        smart_home_routes,
        "discover_home_assistant",
        AsyncMock(return_value="http://homeassistant.local:8123"),
    )

    result = await smart_home_routes.discover_smart_home()
    assert result.found is True
    assert result.url == "http://homeassistant.local:8123"


@pytest.mark.asyncio
async def test_authorize_route_returns_authorize_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import Request

    monkeypatch.setattr(
        smart_home_routes,
        "assert_allowed_oauth_origin",
        lambda origin, request_origin=None: origin.rstrip("/"),
    )
    request = smart_home_routes.HaAuthorizeRequest(
        url="http://homeassistant.local:8123",
        origin="http://127.0.0.1:1420",
    )
    http_request = AsyncMock(spec=Request)
    http_request.headers = {"origin": "http://127.0.0.1:1420"}

    result = await smart_home_routes.authorize_smart_home(request, http_request)
    assert result.ha_url == "http://homeassistant.local:8123"
    assert "/auth/authorize?" in result.authorize_url


@pytest.mark.asyncio
async def test_auth_callback_success_publishes_event(monkeypatch: pytest.MonkeyPatch) -> None:
    flow = PendingHaAuthFlow(
        ha_url="http://127.0.0.1:8123",
        client_id="http://127.0.0.1:1420/",
        expires_at=9_999_999_999,
    )
    monkeypatch.setattr(smart_home_routes, "consume_ha_auth_flow", lambda state: flow)
    monkeypatch.setattr(smart_home_routes, "complete_ha_auth_flow", AsyncMock(return_value=flow.ha_url))
    publish = AsyncMock()
    monkeypatch.setattr(smart_home_routes, "publish_oauth_changed", publish)

    response = await smart_home_routes.smart_home_auth_callback(code="abc", state="home_assistant:nonce")
    assert isinstance(response, HTMLResponse)
    publish.assert_awaited_once()
    assert publish.await_args.kwargs["success"] is True
    assert "kind" not in publish.await_args.kwargs


@pytest.mark.asyncio
async def test_auth_callback_stale_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smart_home_routes, "consume_ha_auth_flow", lambda state: None)
    publish = AsyncMock()
    monkeypatch.setattr(smart_home_routes, "publish_oauth_changed", publish)

    response = await smart_home_routes.smart_home_auth_callback(code="abc", state="home_assistant:stale")
    assert isinstance(response, HTMLResponse)
    publish.assert_awaited_once()
    assert publish.await_args.kwargs["success"] is False
