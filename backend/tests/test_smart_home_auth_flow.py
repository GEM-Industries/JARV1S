"""Tests for Home Assistant browser-authorization connect flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from plugins.smart_home import auth_flow
from plugins.smart_home.ha_client import HA_TOKEN_CLIENT_NAME, AuthTokenResult


@pytest.fixture(autouse=True)
def _clear_pending() -> None:
    auth_flow.clear_pending_for_tests()


def test_issue_and_consume_ha_auth_flow_is_single_use() -> None:
    started = auth_flow.issue_ha_auth_flow(
        ha_url="http://homeassistant.local:8123",
        origin="http://127.0.0.1:1420",
    )
    assert "auth/authorize" in started.authorize_url
    assert "response_type=code" in started.authorize_url
    assert "client_id=http%3A%2F%2F127.0.0.1%3A1420%2F" in started.authorize_url

    flow = auth_flow.consume_ha_auth_flow(started.state)
    assert flow is not None
    assert flow.ha_url == "http://homeassistant.local:8123"
    assert flow.client_id == "http://127.0.0.1:1420/"
    assert auth_flow.consume_ha_auth_flow(started.state) is None


def test_consume_rejects_invalid_state() -> None:
    assert auth_flow.consume_ha_auth_flow("bad") is None
    assert auth_flow.consume_ha_auth_flow("google:abc") is None


def test_consume_rejects_expired_flow() -> None:
    started = auth_flow.issue_ha_auth_flow(
        ha_url="http://127.0.0.1:8123",
        origin="http://127.0.0.1:1420",
    )
    flow = auth_flow._pending[started.state.split(":", 1)[1]]
    auth_flow._pending[started.state.split(":", 1)[1]] = auth_flow.PendingHaAuthFlow(
        ha_url=flow.ha_url,
        client_id=flow.client_id,
        expires_at=0,
    )
    assert auth_flow.consume_ha_auth_flow(started.state) is None


@pytest.mark.asyncio
async def test_complete_ha_auth_flow_persists_long_lived_and_revokes(monkeypatch: pytest.MonkeyPatch) -> None:
    started = auth_flow.issue_ha_auth_flow(
        ha_url="http://127.0.0.1:8123",
        origin="http://127.0.0.1:1420",
    )
    flow = auth_flow.consume_ha_auth_flow(started.state)
    assert flow is not None

    client = MagicMock()
    client.exchange_auth_code = AsyncMock(
        return_value=AuthTokenResult(access_token="short", refresh_token="refresh")
    )
    client.create_long_lived_access_token = AsyncMock(return_value="ll-token")
    client.revoke_refresh_token = AsyncMock()
    client.aclose = AsyncMock()

    persist = AsyncMock()
    monkeypatch.setattr(auth_flow, "HomeAssistantClient", lambda **kwargs: client)
    monkeypatch.setattr(auth_flow, "persist_ha_connection", persist)

    url = await auth_flow.complete_ha_auth_flow(code="abc", flow=flow)

    assert url == "http://127.0.0.1:8123"
    client.exchange_auth_code.assert_awaited_once_with("abc", client_id=flow.client_id)
    client.create_long_lived_access_token.assert_awaited_once_with(HA_TOKEN_CLIENT_NAME)
    persist.assert_awaited_once_with("http://127.0.0.1:8123", "ll-token")
    client.revoke_refresh_token.assert_awaited_once_with("refresh")


@pytest.mark.asyncio
async def test_complete_ha_auth_flow_revokes_refresh_token_when_persist_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = auth_flow.issue_ha_auth_flow(
        ha_url="http://127.0.0.1:8123",
        origin="http://127.0.0.1:1420",
    )
    flow = auth_flow.consume_ha_auth_flow(started.state)
    assert flow is not None

    client = MagicMock()
    client.exchange_auth_code = AsyncMock(
        return_value=AuthTokenResult(access_token="short", refresh_token="refresh")
    )
    client.create_long_lived_access_token = AsyncMock(return_value="ll-token")
    client.revoke_refresh_token = AsyncMock()
    client.aclose = AsyncMock()
    monkeypatch.setattr(auth_flow, "HomeAssistantClient", lambda **kwargs: client)
    monkeypatch.setattr(
        auth_flow,
        "persist_ha_connection",
        AsyncMock(side_effect=RuntimeError("store unavailable")),
    )

    with pytest.raises(RuntimeError, match="store unavailable"):
        await auth_flow.complete_ha_auth_flow(code="abc", flow=flow)

    client.revoke_refresh_token.assert_awaited_once_with("refresh")
    client.aclose.assert_awaited_once()
