import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.auth.device_service import InvalidWsTicketError, device_auth_service
from core.config import settings

from .connection import manager
from .handlers import handle_message
from .models import WSMessage
from .presence import build_presence_from_auth, build_presence_identity

logger = logging.getLogger(__name__)
router = APIRouter()

WS_AUTH_CLOSE_CODE = 1008


def _presence_values(
    *,
    node_id: Optional[str],
    node_label: Optional[str],
    capabilities: Optional[str],
    client_surface: Optional[str],
    location_provider: Optional[str],
    room_id: Optional[str],
    room_name: Optional[str],
    ha_area_id: Optional[str],
    ha_device_id: Optional[str],
    ha_entity_id: Optional[str],
) -> dict[str, str | None]:
    return {
        "node_id": node_id,
        "node_label": node_label,
        "capabilities": capabilities,
        "client_surface": client_surface,
        "location_provider": location_provider,
        "room_id": room_id,
        "room_name": room_name,
        "ha_area_id": ha_area_id,
        "ha_device_id": ha_device_id,
        "ha_entity_id": ha_entity_id,
    }


def _client_host(websocket: WebSocket) -> str | None:
    client = getattr(websocket, "client", None)
    peer = client.host if client else None
    if not device_auth_service.is_loopback_host(peer):
        return peer
    forwarded = websocket.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return peer


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    timezone: str = "UTC",
    ticket: Optional[str] = None,
    node_id: Optional[str] = None,
    node_label: Optional[str] = None,
    capabilities: Optional[str] = None,
    client_surface: Optional[str] = None,
    location_provider: Optional[str] = None,
    room_id: Optional[str] = None,
    room_name: Optional[str] = None,
    ha_area_id: Optional[str] = None,
    ha_device_id: Optional[str] = None,
    ha_entity_id: Optional[str] = None,
):
    """WebSocket endpoint for real-time communication."""
    values = _presence_values(
        node_id=node_id,
        node_label=node_label,
        capabilities=capabilities,
        client_surface=client_surface,
        location_provider=location_provider,
        room_id=room_id,
        room_name=room_name,
        ha_area_id=ha_area_id,
        ha_device_id=ha_device_id,
        ha_entity_id=ha_entity_id,
    )
    client_host = _client_host(websocket)
    origin = websocket.headers.get("origin")
    local_bypass = device_auth_service.local_bypass_allowed(host=client_host)

    if origin and not device_auth_service.origin_allowed(
        origin,
        request_host=websocket.headers.get("host"),
    ):
        await websocket.close(code=WS_AUTH_CLOSE_CODE, reason="origin rejected")
        return

    if settings.DEVICE_AUTH_REQUIRED and not local_bypass:
        if not ticket:
            await websocket.close(code=WS_AUTH_CLOSE_CODE, reason="auth required")
            return
        try:
            # Tickets are single-use even if the socket drops before Session creation;
            # clients must mint a fresh ticket for each reconnect attempt.
            auth_result = await device_auth_service.authenticate_ws_ticket(ticket)
        except InvalidWsTicketError:
            await websocket.close(code=WS_AUTH_CLOSE_CODE, reason="invalid ticket")
            return
        presence = build_presence_from_auth(auth_result, values)
    else:
        from core.auth.device_models import device_kind_override_for_client_surface

        bypass_kind = (
            device_kind_override_for_client_surface(values.get("client_surface"))
            or device_auth_service.local_bypass_device_kind()
        )
        presence = build_presence_identity(
            values,
            device_kind=bypass_kind,
        )

    connection_id = presence.connection_id

    try:
        await manager.connect(websocket, presence, timezone=timezone)

        while True:
            try:
                data = await websocket.receive_json()
                message = WSMessage.model_validate(data)
                message.session_id = connection_id

                await handle_message(connection_id, message)

            except ValueError as e:
                logger.error("Invalid message format: %s", e)
                continue

    except WebSocketDisconnect as e:
        logger.info(
            "WebSocketDisconnect received: connection=%s code=%s reason=%s",
            connection_id,
            getattr(e, "code", None),
            getattr(e, "reason", ""),
        )
        await manager.disconnect(connection_id, websocket)
    except Exception as e:
        logger.error("WebSocket error: %s", e)
        await manager.disconnect(connection_id, websocket)
