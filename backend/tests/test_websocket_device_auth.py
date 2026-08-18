from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from starlette.websockets import WebSocketDisconnect

from api.websockets.routes import websocket_endpoint
from core.auth.device_models import DeviceAuthResult, DeviceLocation


class FakeWebSocket:
    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        client_host: str | None = None,
    ) -> None:
        self.headers = headers or {}
        self.client = SimpleNamespace(host=client_host) if client_host else None
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def receive_json(self):
        raise WebSocketDisconnect()


@pytest.mark.asyncio
async def test_websocket_rejects_missing_ticket_when_auth_required(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)

    ws = FakeWebSocket(headers={"host": "192.168.1.10:8000"})
    with patch("api.websockets.routes.manager") as mock_manager:
        await websocket_endpoint(ws, timezone="UTC")
    assert ws.closed is True
    assert ws.close_code == 1008
    mock_manager.connect.assert_not_called()


@pytest.mark.asyncio
async def test_websocket_does_not_bypass_forwarded_remote_client(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", True)
    monkeypatch.setattr(type(settings), "is_development", property(lambda self: True))
    monkeypatch.delenv("JARVIS_APP_MODE", raising=False)
    ws = FakeWebSocket(
        headers={"x-forwarded-for": "100.64.0.20"},
        client_host="127.0.0.1",
    )

    with patch("api.websockets.routes.manager") as mock_manager:
        await websocket_endpoint(ws, timezone="UTC")

    assert ws.close_code == 1008
    mock_manager.connect.assert_not_called()


@pytest.mark.asyncio
async def test_packaged_host_bypass_is_desktop_and_checks_origin(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)
    monkeypatch.setenv("JARVIS_APP_MODE", "1")
    ws = FakeWebSocket(
        headers={
            "host": "127.0.0.1:8000",
            "origin": "http://127.0.0.1:8000",
        },
        client_host="127.0.0.1",
    )

    with (
        patch("api.websockets.routes.manager.connect", new=AsyncMock()) as connect,
        patch("api.websockets.routes.manager.disconnect", new=AsyncMock()),
    ):
        await websocket_endpoint(ws, timezone="UTC", node_id="host-node")

    presence = connect.await_args.args[1]
    assert presence.device_kind == "desktop"

    rejected = FakeWebSocket(
        headers={
            "host": "127.0.0.1:8000",
            "origin": "https://evil.example",
        },
        client_host="127.0.0.1",
    )
    with patch("api.websockets.routes.manager") as manager:
        await websocket_endpoint(rejected, timezone="UTC")

    assert rejected.close_reason == "origin rejected"
    manager.connect.assert_not_called()


@pytest.mark.asyncio
async def test_websocket_accepts_valid_ticket_before_connect(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)

    auth = DeviceAuthResult(
        device_id="dev-1",
        owner_id="owner-1",
        node_id="browser-1",
        capabilities=["mic", "speaker", "display"],
        location=DeviceLocation(),
        kind="browser",
    )
    ws = FakeWebSocket(
        headers={"host": "192.168.1.10:8000", "origin": settings.FRONTEND_ORIGIN}
    )

    with (
        patch(
            "api.websockets.routes.device_auth_service.authenticate_ws_ticket",
            new=AsyncMock(return_value=auth),
        ),
        patch(
            "api.websockets.routes.device_auth_service.origin_allowed",
            return_value=True,
        ),
        patch("api.websockets.routes.manager.connect", new=AsyncMock()) as connect,
        patch("api.websockets.routes.manager.disconnect", new=AsyncMock()),
    ):
        await websocket_endpoint(
            ws, timezone="UTC", ticket="ticket-abc", node_id="browser-1"
        )

    connect.assert_awaited_once()
    presence = connect.await_args.args[1]
    assert presence.owner_id == "owner-1"
    assert presence.node_id == "browser-1"
    assert presence.device_kind == "browser"


@pytest.mark.asyncio
async def test_websocket_uses_credential_kind_not_client_surface_hint(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)
    auth = DeviceAuthResult(
        device_id="dev-1",
        owner_id="owner-1",
        node_id="phone-1",
        capabilities=["mic", "speaker", "display"],
        location=DeviceLocation(),
        kind="phone",
    )
    ws = FakeWebSocket(
        headers={"host": "192.168.1.10:8000", "origin": settings.FRONTEND_ORIGIN}
    )
    update_kind = AsyncMock(return_value=True)

    with (
        patch(
            "api.websockets.routes.device_auth_service.authenticate_ws_ticket",
            new=AsyncMock(return_value=auth),
        ),
        patch(
            "api.websockets.routes.device_auth_service.update_device_kind",
            new=update_kind,
        ),
        patch(
            "api.websockets.routes.device_auth_service.origin_allowed",
            return_value=True,
        ),
        patch("api.websockets.routes.manager.connect", new=AsyncMock()) as connect,
        patch("api.websockets.routes.manager.disconnect", new=AsyncMock()),
    ):
        await websocket_endpoint(
            ws,
            timezone="UTC",
            ticket="ticket-abc",
            node_id="phone-1",
            client_surface="desktop_app",
        )

    update_kind.assert_not_awaited()
    presence = connect.await_args.args[1]
    assert presence.device_kind == "phone"
    assert presence.context()["device_kind"] == "phone"


@pytest.mark.asyncio
async def test_websocket_rejects_origin_before_consuming_ticket(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "DEVICE_AUTH_REQUIRED", True)
    monkeypatch.setattr(settings, "DEVICE_AUTH_DEV_BYPASS", False)

    ws = FakeWebSocket(
        headers={"host": "192.168.1.10:8000", "origin": "https://evil.example"}
    )
    authenticate = AsyncMock()
    with (
        patch(
            "api.websockets.routes.device_auth_service.origin_allowed",
            return_value=False,
        ),
        patch(
            "api.websockets.routes.device_auth_service.authenticate_ws_ticket",
            new=authenticate,
        ),
        patch("api.websockets.routes.manager") as mock_manager,
    ):
        await websocket_endpoint(ws, timezone="UTC", ticket="ticket-abc")

    assert ws.closed is True
    assert ws.close_reason == "origin rejected"
    authenticate.assert_not_awaited()
    mock_manager.connect.assert_not_called()
