"""Tests for Home Assistant product config resolution and connect API."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.routes import smart_home as smart_home_routes
from plugins.smart_home.config import (
    clear_ha_connection,
    ha_config_store,
    is_ha_configured,
    resolve_ha_connection_sync,
)
from plugins.smart_home.ha_client import HA_TOKEN_CLIENT_NAME
from plugins.smart_home.status import LivenessStatus
from plugins.smart_home.ui_status import SmartHomeUiStatus, build_smart_home_status


@pytest.fixture(autouse=True)
def _reset_ha_config_cache() -> None:
    ha_config_store.clear_cache()
    yield
    ha_config_store.clear_cache()


def test_resolve_prefers_persisted_url_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    ha_config_store._cache = {"url": "http://ha.local:8123"}
    monkeypatch.setattr(
        "plugins.smart_home.config.credential_store.get_secret",
        lambda key: {
            "HA_URL": "http://127.0.0.1:8123",
            "HA_TOKEN": "token",
        }.get(key),
    )

    url, token = resolve_ha_connection_sync()

    assert url == "http://ha.local:8123"
    assert token == "token"
    assert is_ha_configured() is True


def test_resolve_falls_back_to_env_when_no_persisted_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "plugins.smart_home.config.credential_store.get_secret",
        lambda key: {
            "HA_URL": "http://127.0.0.1:8123",
            "HA_TOKEN": "token",
        }.get(key),
    )

    url, token = resolve_ha_connection_sync()

    assert url == "http://127.0.0.1:8123"
    assert token == "token"


def test_is_ha_configured_false_when_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    ha_config_store._cache = {"url": "http://ha.local:8123"}
    monkeypatch.setattr(
        "plugins.smart_home.config.credential_store.get_secret",
        lambda key: "http://ha.local:8123" if key == "HA_URL" else None,
    )

    assert is_ha_configured() is False


def test_resolve_ignores_env_after_product_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    ha_config_store._cache = {"disconnected": True}
    monkeypatch.setattr(
        "plugins.smart_home.config.credential_store.get_secret",
        lambda key: {
            "HA_URL": "http://127.0.0.1:8123",
            "HA_TOKEN": "token",
        }.get(key),
    )

    url, token = resolve_ha_connection_sync()

    assert url is None
    assert token is None
    assert is_ha_configured() is False


@pytest.mark.asyncio
async def test_build_status_unconfigured_uses_product_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "plugins.smart_home.ui_status.resolve_ha_connection",
        AsyncMock(return_value=(None, None)),
    )

    result = await build_smart_home_status()

    assert result.status == SmartHomeUiStatus.UNCONFIGURED
    assert "look for Home Assistant" in (result.next_action or "")


@pytest.mark.asyncio
async def test_connect_route_persists_and_returns_status(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = smart_home_routes.SmartHomeStatusResponse(
        status=SmartHomeUiStatus.READY,
        message="ready",
    )
    liveness = LivenessStatus(
        configured=True,
        reachable=True,
        authenticated=True,
        message="ok",
    )

    persist = AsyncMock()
    monkeypatch.setattr(smart_home_routes, "validate_ha_connection", AsyncMock(return_value=liveness))
    monkeypatch.setattr(smart_home_routes, "persist_ha_connection", persist)
    monkeypatch.setattr(smart_home_routes, "build_smart_home_status", AsyncMock(return_value=expected))

    result = await smart_home_routes.connect_smart_home(
        smart_home_routes.HaConnectRequest(url="http://127.0.0.1:8123", token="secret")
    )

    persist.assert_awaited_once_with("http://127.0.0.1:8123", "secret")
    assert result.status == SmartHomeUiStatus.READY


@pytest.mark.asyncio
async def test_connect_route_rejects_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    liveness = LivenessStatus(
        configured=True,
        reachable=True,
        authenticated=False,
        message="Home Assistant rejected the access token",
    )
    monkeypatch.setattr(smart_home_routes, "validate_ha_connection", AsyncMock(return_value=liveness))

    with pytest.raises(HTTPException) as exc:
        await smart_home_routes.connect_smart_home(
            smart_home_routes.HaConnectRequest(url="http://127.0.0.1:8123", token="bad")
        )

    assert "rejected" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_disconnect_route_clears_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    clear = AsyncMock()
    expected = smart_home_routes.SmartHomeStatusResponse(
        status=SmartHomeUiStatus.UNCONFIGURED,
        message="not connected",
    )
    monkeypatch.setattr(smart_home_routes, "clear_ha_connection", clear)
    monkeypatch.setattr(smart_home_routes, "build_smart_home_status", AsyncMock(return_value=expected))

    result = await smart_home_routes.disconnect_smart_home()

    clear.assert_awaited_once()
    assert result.status == SmartHomeUiStatus.UNCONFIGURED


@pytest.mark.asyncio
async def test_clear_ha_connection_revokes_remote_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = AsyncMock()
    client.delete_long_lived_access_tokens = AsyncMock(return_value=1)
    client.aclose = AsyncMock()
    deleted: list[str] = []

    monkeypatch.setattr(
        "plugins.smart_home.config.resolve_ha_connection",
        AsyncMock(return_value=("http://127.0.0.1:8123", "token")),
    )
    monkeypatch.setattr(
        "plugins.smart_home.config.HomeAssistantClient",
        lambda **kwargs: client,
    )
    monkeypatch.setattr(
        "plugins.smart_home.config.ha_config_store.clear",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "plugins.smart_home.config.credential_store.delete_secret",
        lambda key: deleted.append(key),
    )
    monkeypatch.setattr(
        "core.integrations.manager.integrations.reset",
        AsyncMock(),
    )

    await clear_ha_connection()

    client.delete_long_lived_access_tokens.assert_awaited_once_with(HA_TOKEN_CLIENT_NAME)
    client.aclose.assert_awaited_once()
    assert deleted == ["HA_TOKEN"]


@pytest.mark.asyncio
async def test_clear_ha_connection_still_clears_when_revoke_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    client.delete_long_lived_access_tokens = AsyncMock(side_effect=RuntimeError("offline"))
    client.aclose = AsyncMock()
    cleared = AsyncMock()
    deleted: list[str] = []

    monkeypatch.setattr(
        "plugins.smart_home.config.resolve_ha_connection",
        AsyncMock(return_value=("http://127.0.0.1:8123", "token")),
    )
    monkeypatch.setattr(
        "plugins.smart_home.config.HomeAssistantClient",
        lambda **kwargs: client,
    )
    monkeypatch.setattr("plugins.smart_home.config.ha_config_store.clear", cleared)
    monkeypatch.setattr(
        "plugins.smart_home.config.credential_store.delete_secret",
        lambda key: deleted.append(key),
    )
    monkeypatch.setattr(
        "core.integrations.manager.integrations.reset",
        AsyncMock(),
    )

    await clear_ha_connection()

    cleared.assert_awaited_once()
    assert deleted == ["HA_TOKEN"]
    client.aclose.assert_awaited_once()
