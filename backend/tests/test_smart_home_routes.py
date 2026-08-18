"""REST route tests for Home Assistant visibility."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import api.routes.smart_home as smart_home_routes
from plugins.smart_home.ui_status import (
    SmartHomeStatusResponse,
    SmartHomeUiStatus,
    build_smart_home_status,
)


def _mock_ha_connection(monkeypatch: pytest.MonkeyPatch, url: str | None, token: str | None) -> None:
    monkeypatch.setattr(
        "plugins.smart_home.ui_status.resolve_ha_connection",
        AsyncMock(return_value=(url, token)),
    )


@pytest.mark.asyncio
async def test_get_status_delegates_to_builder(monkeypatch):
    expected = SmartHomeStatusResponse(
        status=SmartHomeUiStatus.UNCONFIGURED,
        message="not configured",
    )

    monkeypatch.setattr(
        smart_home_routes,
        "build_smart_home_status",
        AsyncMock(return_value=expected),
    )

    result = await smart_home_routes.get_smart_home_status()
    assert result.status == SmartHomeUiStatus.UNCONFIGURED


@pytest.mark.asyncio
async def test_build_status_unconfigured(monkeypatch):
    _mock_ha_connection(monkeypatch, None, None)

    result = await build_smart_home_status()

    assert result.status == SmartHomeUiStatus.UNCONFIGURED
    assert result.configured is False
    assert "look for Home Assistant" in (result.next_action or "")


@pytest.mark.asyncio
async def test_build_status_invalid_config(monkeypatch):
    _mock_ha_connection(monkeypatch, "http://", "token")

    result = await build_smart_home_status()

    assert result.status == SmartHomeUiStatus.INVALID_CONFIG
    assert result.next_action is not None


@pytest.mark.asyncio
async def test_build_status_unreachable(monkeypatch):
    from plugins.smart_home.status import LivenessStatus

    _mock_ha_connection(monkeypatch, "http://localhost:8123", "token")
    monkeypatch.setattr(
        "plugins.smart_home.ui_status.check_liveness",
        AsyncMock(
            return_value=LivenessStatus(
                configured=True,
                reachable=False,
                authenticated=False,
                message="Cannot reach Home Assistant",
            )
        ),
    )

    result = await build_smart_home_status()

    assert result.status == SmartHomeUiStatus.UNREACHABLE
    assert result.reachable is False


@pytest.mark.asyncio
async def test_build_status_auth_failed(monkeypatch):
    from plugins.smart_home.status import LivenessStatus

    _mock_ha_connection(monkeypatch, "http://localhost:8123", "bad")
    monkeypatch.setattr(
        "plugins.smart_home.ui_status.check_liveness",
        AsyncMock(
            return_value=LivenessStatus(
                configured=True,
                reachable=True,
                authenticated=False,
                message="Home Assistant rejected the access token",
            )
        ),
    )

    result = await build_smart_home_status()

    assert result.status == SmartHomeUiStatus.AUTH_FAILED
    assert result.authenticated is False
    assert "Sign in again" in (result.next_action or "")


@pytest.mark.asyncio
async def test_build_status_ready(monkeypatch):
    from plugins.smart_home.inventory import InventoryEntity, InventorySnapshot
    from plugins.smart_home.status import LivenessStatus, ReadinessStatus

    snapshot = InventorySnapshot(
        captured_at="2026-01-01T00:00:00Z",
        area_count=1,
        device_count=1,
        entity_count=1,
        entities=[
            InventoryEntity(
                entity_id="light.living_room",
                name="Living Room",
                domain="light",
                state="on",
                area_name="Living Room",
            )
        ],
    )
    liveness = LivenessStatus(
        configured=True,
        reachable=True,
        authenticated=True,
        message="ok",
    )
    readiness = ReadinessStatus(
        liveness=liveness,
        registry_access=True,
        entity_count=1,
        safe_controllable_count=1,
        area_count=1,
        device_count=1,
        setup_candidate="light.living_room",
        ready=True,
        message="Home Assistant is ready for device control.",
        snapshot=snapshot,
    )

    _mock_ha_connection(monkeypatch, "http://localhost:8123", "token")
    monkeypatch.setattr(
        "plugins.smart_home.ui_status.check_liveness",
        AsyncMock(return_value=liveness),
    )

    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()

    with patch("plugins.smart_home.ui_status.HomeAssistantClient", return_value=mock_client), \
         patch("plugins.smart_home.ui_status.check_readiness", AsyncMock(return_value=readiness)):
        result = await build_smart_home_status()

    assert result.status == SmartHomeUiStatus.READY
    assert result.safe_controllable_count == 1
    assert len(result.devices) == 1
    assert result.ha_url == "http://localhost:8123"
