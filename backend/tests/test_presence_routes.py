"""REST route and presence merge tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

import api.routes.presence as presence_routes
from api.websockets.connection import (
    DEVICE_DISCONNECTED_CLOSE_CODE,
    DEVICE_REVOKED_CLOSE_CODE,
    ConnectionManager,
)
from api.websockets.presence import build_presence_identity
from core.auth.device_models import DeviceCredentialSummary, DeviceLocation
from core.preferences.models import UserPreferences
from core.presence.models import PresenceCore, PresenceView
from core.presence.service import (
    assign_node_room,
    build_presence_view,
    disconnect_presence_device,
    revoke_presence_device,
)
from tests.test_presence_identity import FakeWebSocket


def _cred(
    *,
    device_id: str,
    node_id: str,
    kind: str = "satellite",
    revoked_at: datetime | None = None,
    disconnected_at: datetime | None = None,
    last_seen_at: datetime | None = None,
    node_label: str | None = None,
    room_name: str | None = None,
    ha_area_id: str | None = None,
) -> DeviceCredentialSummary:
    return DeviceCredentialSummary(
        device_id=device_id,
        owner_id="home",
        node_id=node_id,
        node_label=node_label,
        capabilities=["mic", "speaker"],
        location=DeviceLocation(
            provider="home_assistant" if ha_area_id or room_name else "unknown",
            room_name=room_name,
            room_id=room_name,
            ha_area_id=ha_area_id,
        ),
        kind=kind,
        revoked_at=revoked_at,
        disconnected_at=disconnected_at,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_seen_at=last_seen_at,
    )


@pytest.mark.asyncio
async def test_get_presence_delegates_to_builder(monkeypatch):
    expected = PresenceView(core=PresenceCore(name="JARV1S"), nodes=[])

    monkeypatch.setattr(
        presence_routes,
        "build_presence_view",
        AsyncMock(return_value=expected),
    )

    result = await presence_routes.get_presence(owner_id="home")
    assert result.core.name == "JARV1S"


@pytest.mark.asyncio
async def test_build_presence_view_merges_states(monkeypatch):
    manager = ConnectionManager()
    online_socket = FakeWebSocket()
    online_presence = build_presence_identity(
        {
            "owner_id": "home",
            "node_id": "bedroom-sat",
            "node_label": "Bedroom",
            "capabilities": "mic,speaker",
            "room_name": "Bedroom",
        },
        connection_id="conn-bedroom",
        allow_owner_override=True,
    )

    with patch("api.websockets.connection.TenVADService"), \
        patch("api.websockets.connection.WakeWordService"), \
        patch("api.websockets.connection.SpeechProcessor"), \
        patch("api.websockets.connection.attention_service.get_state", new=AsyncMock(return_value=None)), \
        patch("api.websockets.connection.get_user_preferences", new=AsyncMock(return_value=UserPreferences(owner_id="home"))), \
        patch("api.websockets.connection.collect_widget_snapshots", new=AsyncMock(return_value=[])):
        await manager.connect(online_socket, online_presence, timezone="UTC")
        manager.record_user_turn_activity("conn-bedroom")

    credentials = [
        _cred(
            device_id="dev-bedroom",
            node_id="bedroom-sat",
            node_label="Bedroom",
            room_name="Bedroom",
            ha_area_id="bedroom",
        ),
        _cred(
            device_id="dev-kitchen",
            node_id="kitchen-sat",
            node_label="Kitchen",
            room_name="Kitchen",
            ha_area_id="kitchen",
        ),
        _cred(
            device_id="dev-old",
            node_id="old-phone",
            kind="phone",
            revoked_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ),
    ]

    monkeypatch.setattr(
        "core.presence.service.device_auth_service.list_devices",
        AsyncMock(return_value=credentials),
    )

    view = await build_presence_view("home", manager=manager)

    by_id = {node.node_id: node for node in view.nodes}
    assert len(view.nodes) == 2
    assert by_id["bedroom-sat"].status == "online"
    assert by_id["bedroom-sat"].active is True
    assert by_id["bedroom-sat"].ha_area_id == "bedroom"
    assert by_id["kitchen-sat"].status == "offline"
    assert by_id["kitchen-sat"].ha_area_id == "kitchen"
    assert "old-phone" not in by_id


@pytest.mark.asyncio
async def test_build_presence_view_omits_fully_revoked_offline_nodes(monkeypatch):
    manager = ConnectionManager()
    credentials = [
        _cred(
            device_id="dev-old",
            node_id="old-phone",
            kind="phone",
            revoked_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ),
        _cred(
            device_id="dev-active",
            node_id="phone-current",
            kind="phone",
            last_seen_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
    ]
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.list_devices",
        AsyncMock(return_value=credentials),
    )

    view = await build_presence_view("home", manager=manager)

    assert [node.node_id for node in view.nodes] == ["phone-current"]
    assert view.nodes[0].status == "offline"


@pytest.mark.asyncio
async def test_build_presence_view_live_only_without_credential(monkeypatch):
    manager = ConnectionManager()
    socket = FakeWebSocket()
    presence = build_presence_identity(
        {"owner_id": "home", "node_id": "browser-abc", "capabilities": "mic,speaker,display"},
        connection_id="conn-browser",
        allow_owner_override=True,
    )

    with patch("api.websockets.connection.TenVADService"), \
        patch("api.websockets.connection.WakeWordService"), \
        patch("api.websockets.connection.SpeechProcessor"), \
        patch("api.websockets.connection.attention_service.get_state", new=AsyncMock(return_value=None)), \
        patch("api.websockets.connection.get_user_preferences", new=AsyncMock(return_value=UserPreferences(owner_id="home"))), \
        patch("api.websockets.connection.collect_widget_snapshots", new=AsyncMock(return_value=[])):
        await manager.connect(socket, presence, timezone="UTC")

    monkeypatch.setattr(
        "core.presence.service.device_auth_service.list_devices",
        AsyncMock(return_value=[]),
    )

    view = await build_presence_view("home", manager=manager)

    assert len(view.nodes) == 1
    node = view.nodes[0]
    assert node.node_id == "browser-abc"
    assert node.kind == "browser"
    assert node.status == "online"
    assert node.device_id is None


@pytest.mark.asyncio
async def test_build_presence_view_uses_live_device_kind(monkeypatch):
    manager = ConnectionManager()
    socket = FakeWebSocket()
    presence = build_presence_identity(
        {
            "owner_id": "home",
            "node_id": "browser-existing",
            "client_surface": "desktop_app",
        },
        connection_id="conn-desktop",
        allow_owner_override=True,
        device_kind="desktop",
    )

    with patch("api.websockets.connection.TenVADService"), \
        patch("api.websockets.connection.WakeWordService"), \
        patch("api.websockets.connection.SpeechProcessor"), \
        patch("api.websockets.connection.attention_service.get_state", new=AsyncMock(return_value=None)), \
        patch("api.websockets.connection.get_user_preferences", new=AsyncMock(return_value=UserPreferences(owner_id="home"))), \
        patch("api.websockets.connection.collect_widget_snapshots", new=AsyncMock(return_value=[])):
        await manager.connect(socket, presence, timezone="UTC")

    monkeypatch.setattr(
        "core.presence.service.device_auth_service.list_devices",
        AsyncMock(return_value=[]),
    )

    view = await build_presence_view("home", manager=manager)

    assert view.nodes[0].node_id == "browser-existing"
    assert view.nodes[0].kind == "desktop"


@pytest.mark.asyncio
async def test_build_presence_view_marks_disconnected_offline_nodes(monkeypatch):
    manager = ConnectionManager()
    credentials = [
        _cred(
            device_id="dev-bedroom",
            node_id="bedroom-sat",
            node_label="Bedroom",
            disconnected_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
        ),
    ]
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.list_devices",
        AsyncMock(return_value=credentials),
    )

    view = await build_presence_view("home", manager=manager)

    assert view.nodes[0].status == "offline"
    assert view.nodes[0].disconnected is True


@pytest.mark.asyncio
async def test_revoke_presence_device_disconnects_live_session(monkeypatch):
    manager = ConnectionManager()
    socket = FakeWebSocket()
    presence = build_presence_identity(
        {"owner_id": "home", "node_id": "kitchen-sat", "capabilities": "mic,speaker"},
        connection_id="conn-kitchen",
        allow_owner_override=True,
    )

    with patch("api.websockets.connection.TenVADService"), \
        patch("api.websockets.connection.WakeWordService"), \
        patch("api.websockets.connection.SpeechProcessor"), \
        patch("api.websockets.connection.attention_service.get_state", new=AsyncMock(return_value=None)), \
        patch("api.websockets.connection.get_user_preferences", new=AsyncMock(return_value=UserPreferences(owner_id="home"))), \
        patch("api.websockets.connection.collect_widget_snapshots", new=AsyncMock(return_value=[])):
        await manager.connect(socket, presence, timezone="UTC")

    target = _cred(device_id="dev-kitchen", node_id="kitchen-sat")
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.list_devices",
        AsyncMock(return_value=[target]),
    )
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.revoke_device",
        AsyncMock(return_value=True),
    )

    revoked = await revoke_presence_device("dev-kitchen", owner_id="home", manager=manager)

    assert revoked is True
    assert socket.closed is True
    assert socket.close_code == DEVICE_REVOKED_CLOSE_CODE
    assert socket.close_reason == "device_revoked"
    assert manager.get_session("conn-kitchen") is None


@pytest.mark.asyncio
async def test_disconnect_presence_device_drops_live_session_without_revoke(monkeypatch):
    manager = ConnectionManager()
    socket = FakeWebSocket()
    presence = build_presence_identity(
        {"owner_id": "home", "node_id": "kitchen-sat", "capabilities": "mic,speaker"},
        connection_id="conn-kitchen",
        allow_owner_override=True,
    )

    with patch("api.websockets.connection.TenVADService"), \
        patch("api.websockets.connection.WakeWordService"), \
        patch("api.websockets.connection.SpeechProcessor"), \
        patch("api.websockets.connection.attention_service.get_state", new=AsyncMock(return_value=None)), \
        patch("api.websockets.connection.get_user_preferences", new=AsyncMock(return_value=UserPreferences(owner_id="home"))), \
        patch("api.websockets.connection.collect_widget_snapshots", new=AsyncMock(return_value=[])):
        await manager.connect(socket, presence, timezone="UTC")

    target = _cred(device_id="dev-kitchen", node_id="kitchen-sat")
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.list_devices",
        AsyncMock(return_value=[target]),
    )
    disconnect_device = AsyncMock(return_value=True)
    revoke_device = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.disconnect_device",
        disconnect_device,
    )
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.revoke_device",
        revoke_device,
    )

    held = await disconnect_presence_device("dev-kitchen", owner_id="home", manager=manager)

    assert held is True
    disconnect_device.assert_awaited_once_with("dev-kitchen")
    revoke_device.assert_not_called()
    assert socket.closed is True
    assert socket.close_code == DEVICE_DISCONNECTED_CLOSE_CODE
    assert socket.close_reason == "device_disconnected"
    assert manager.get_session("conn-kitchen") is None


@pytest.mark.asyncio
async def test_assign_node_room_updates_credential_and_live_presence(monkeypatch):
    manager = ConnectionManager()
    socket = FakeWebSocket()
    presence = build_presence_identity(
        {"owner_id": "home", "node_id": "bedroom-sat", "capabilities": "mic,speaker"},
        connection_id="conn-bedroom",
        allow_owner_override=True,
    )

    with patch("api.websockets.connection.TenVADService"), \
        patch("api.websockets.connection.WakeWordService"), \
        patch("api.websockets.connection.SpeechProcessor"), \
        patch("api.websockets.connection.attention_service.get_state", new=AsyncMock(return_value=None)), \
        patch("api.websockets.connection.get_user_preferences", new=AsyncMock(return_value=UserPreferences(owner_id="home"))), \
        patch("api.websockets.connection.collect_widget_snapshots", new=AsyncMock(return_value=[])), \
        patch("api.websockets.connection.event_bus.publish", new=AsyncMock()):
        await manager.connect(socket, presence, timezone="UTC")

    location_ref = DeviceLocation()

    async def _update_node_location(*, owner_id: str, node_id: str, location: DeviceLocation) -> int:
        nonlocal location_ref
        location_ref = location
        return 1

    async def _list_devices(owner_id: str):
        return [
            _cred(
                device_id="dev-bedroom",
                node_id="bedroom-sat",
                node_label="Bedroom Satellite",
                room_name=location_ref.room_name,
                ha_area_id=location_ref.ha_area_id,
            )
        ]

    monkeypatch.setattr(
        "core.presence.service.device_auth_service.update_node_location",
        AsyncMock(side_effect=_update_node_location),
    )
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.list_devices",
        AsyncMock(side_effect=_list_devices),
    )

    view = await assign_node_room(
        "bedroom-sat",
        owner_id="home",
        ha_area_id="bedroom",
        area_name="Bedroom",
        manager=manager,
    )

    node = next(item for item in view.nodes if item.node_id == "bedroom-sat")
    assert node.room_name == "Bedroom"
    assert node.ha_area_id == "bedroom"
    session = manager.get_session_by_connection("conn-bedroom")
    assert session is not None
    assert session.presence.location.ha_area_id == "bedroom"


@pytest.mark.asyncio
async def test_assign_node_room_can_clear_binding(monkeypatch):
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.update_node_location",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.list_devices",
        AsyncMock(return_value=[_cred(device_id="dev-bedroom", node_id="bedroom-sat")]),
    )

    view = await assign_node_room(
        "bedroom-sat",
        owner_id="home",
        ha_area_id=None,
        area_name=None,
        manager=ConnectionManager(),
    )

    node = next(item for item in view.nodes if item.node_id == "bedroom-sat")
    assert node.room_name is None
    assert node.ha_area_id is None


@pytest.mark.asyncio
async def test_assign_node_room_rejects_unpaired_endpoint(monkeypatch):
    monkeypatch.setattr(
        "core.presence.service.device_auth_service.update_node_location",
        AsyncMock(return_value=0),
    )

    with pytest.raises(ValueError, match="Pair this endpoint"):
        await assign_node_room(
            "browser-dev",
            owner_id="home",
            ha_area_id="office",
            area_name="Office",
            manager=ConnectionManager(),
        )


@pytest.mark.asyncio
async def test_revoke_route_404_when_not_found(monkeypatch):
    monkeypatch.setattr(
        presence_routes,
        "revoke_presence_device",
        AsyncMock(return_value=False),
    )

    with pytest.raises(HTTPException) as exc:
        await presence_routes.revoke_device("missing-dev", owner_id="home")

    assert exc.value.status_code == 404
