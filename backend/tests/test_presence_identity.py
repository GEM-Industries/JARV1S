from unittest.mock import AsyncMock, patch

import pytest

from api.websockets.connection import ConnectionManager, NODE_REPLACED_CLOSE_CODE
from api.websockets.presence import build_presence_identity
from core.preferences.models import UserPreferences


class FakeWebSocket:
    def __init__(self) -> None:
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.sent: list[str] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


def test_presence_defaults_to_owner_and_default_node():
    presence = build_presence_identity({})

    assert presence.owner_id
    assert presence.node_id == "default"
    assert presence.capabilities == frozenset({"mic", "speaker", "display"})
    assert presence.device_kind == "browser"
    assert presence.location.provider == "unknown"


def test_presence_does_not_trust_client_owner_by_default():
    presence = build_presence_identity({"owner_id": "forged"})

    assert presence.owner_id != "forged"


def test_presence_uses_server_classified_device_kind_on_bypass():
    presence = build_presence_identity(
        {"node_id": "browser-existing", "client_surface": "phone"},
        device_kind="desktop",
    )

    assert presence.node_id == "browser-existing"
    assert presence.device_kind == "desktop"
    assert presence.context()["device_kind"] == "desktop"


def test_presence_accepts_home_assistant_location_refs():
    presence = build_presence_identity(
        {
            "owner_id": "home",
            "node_id": "office-pi",
            "node_label": "Office Pi",
            "capabilities": "mic,speaker",
            "location_provider": "home_assistant",
            "room_id": "office",
            "room_name": "Office",
            "ha_area_id": "area-1",
            "ha_device_id": "device-1",
            "ha_entity_id": "assist_satellite.office",
        },
        allow_owner_override=True,
    )

    assert presence.owner_id == "home"
    assert presence.node_id == "office-pi"
    assert presence.node_label == "Office Pi"
    assert presence.capabilities == frozenset({"mic", "speaker"})
    assert presence.location.provider == "home_assistant"
    assert presence.location.ha_entity_id == "assist_satellite.office"


@pytest.mark.asyncio
async def test_connection_manager_keys_by_connection_and_resolves_owner():
    manager = ConnectionManager()
    first_socket = FakeWebSocket()
    second_socket = FakeWebSocket()
    first = build_presence_identity(
        {"owner_id": "home", "node_id": "office"},
        connection_id="conn-first",
        allow_owner_override=True,
    )
    second = build_presence_identity(
        {"owner_id": "home", "node_id": "office"},
        connection_id="conn-second",
        allow_owner_override=True,
    )

    with (
        patch("api.websockets.connection.TenVADService"),
        patch("api.websockets.connection.WakeWordService"),
        patch("api.websockets.connection.SpeechProcessor"),
        patch(
            "api.websockets.connection.get_user_preferences",
            new=AsyncMock(return_value=UserPreferences(owner_id="home")),
        ),
        patch(
            "api.websockets.connection.attention_service.get_state",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "api.websockets.connection.collect_widget_snapshots",
            new=AsyncMock(return_value=[]),
        ),
    ):
        await manager.connect(first_socket, first, timezone="Australia/Sydney")
        assert manager.get_session("conn-first") is not None
        assert manager.get_session("home") is manager.get_session("conn-first")

        await manager.connect(second_socket, second, timezone="Australia/Sydney")
        await manager.disconnect("conn-first")

    assert first_socket.closed is True
    assert first_socket.close_code == NODE_REPLACED_CLOSE_CODE
    assert first_socket.close_reason == "node_replaced"
    assert manager.get_session("conn-first") is None
    assert manager.get_session("home") is manager.get_session("conn-second")
    assert manager.get_owner_id("conn-second") == "home"
    assert manager.resolve_connection_id("home") == "conn-second"


@pytest.mark.asyncio
async def test_update_node_location_refreshes_live_presence():
    from core.auth.device_models import DeviceLocation

    manager = ConnectionManager()
    bedroom_socket = FakeWebSocket()
    bedroom = build_presence_identity(
        {"owner_id": "home", "node_id": "bedroom-sat"},
        connection_id="conn-bedroom",
        allow_owner_override=True,
    )

    with (
        patch("api.websockets.connection.TenVADService"),
        patch("api.websockets.connection.WakeWordService"),
        patch("api.websockets.connection.SpeechProcessor"),
        patch(
            "api.websockets.connection.get_user_preferences",
            new=AsyncMock(return_value=UserPreferences(owner_id="home")),
        ),
        patch(
            "api.websockets.connection.attention_service.get_state",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "api.websockets.connection.collect_widget_snapshots",
            new=AsyncMock(return_value=[]),
        ),
        patch("api.websockets.connection.event_bus.publish", new=AsyncMock()),
    ):
        await manager.connect(bedroom_socket, bedroom, timezone="UTC")

    location = DeviceLocation(
        provider="home_assistant",
        room_id="bedroom",
        room_name="Bedroom",
        ha_area_id="area-bedroom",
    )
    assert manager.update_node_location("home", "bedroom-sat", location) is True
    session = manager.get_session_by_connection("conn-bedroom")
    assert session is not None
    assert session.presence.location.ha_area_id == "area-bedroom"
    endpoints = manager.list_live_endpoints("home")
    assert endpoints[0].location.ha_area_id == "area-bedroom"


@pytest.mark.asyncio
async def test_two_distinct_nodes_coexist_for_same_owner():
    manager = ConnectionManager()
    kitchen_socket = FakeWebSocket()
    browser_socket = FakeWebSocket()
    kitchen = build_presence_identity(
        {"owner_id": "home", "node_id": "kitchen"},
        connection_id="conn-kitchen",
        allow_owner_override=True,
    )
    browser = build_presence_identity(
        {"owner_id": "home", "node_id": "browser"},
        connection_id="conn-browser",
        allow_owner_override=True,
    )

    with (
        patch("api.websockets.connection.TenVADService"),
        patch("api.websockets.connection.WakeWordService"),
        patch("api.websockets.connection.SpeechProcessor"),
        patch(
            "api.websockets.connection.get_user_preferences",
            new=AsyncMock(return_value=UserPreferences(owner_id="home")),
        ),
        patch(
            "api.websockets.connection.attention_service.get_state",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "api.websockets.connection.collect_widget_snapshots",
            new=AsyncMock(return_value=[]),
        ),
        patch("api.websockets.connection.event_bus.publish", new=AsyncMock()),
    ):
        await manager.connect(kitchen_socket, kitchen, timezone="UTC")
        await manager.connect(browser_socket, browser, timezone="UTC")

    assert manager.get_session_by_connection("conn-kitchen") is not None
    assert manager.get_session_by_connection("conn-browser") is not None
    assert manager.get_default_session_for_owner("home").connection_id == "conn-browser"
    assert manager.get_session_by_connection("home") is None
