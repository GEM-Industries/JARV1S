"""Merge live WebSocket presence with provisioned device credentials."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from core.auth.device_models import DeviceCredentialSummary, DeviceKind, DeviceLocation
from core.auth.device_service import device_auth_service
from core.config import settings
from core.presence.models import PresenceCore, PresenceNode, PresenceView
from core.triggers.endpoint_router import _pick_best

if TYPE_CHECKING:
    from api.websockets.connection import ConnectionManager, Session


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _room_name_from_session(session: Session) -> str | None:
    loc = session.presence.location
    return loc.room_name or loc.room_id


def _room_name_from_credential(cred: DeviceCredentialSummary) -> str | None:
    return cred.location.room_name or cred.location.room_id


def _location_for_ha_area(ha_area_id: str | None, area_name: str | None) -> DeviceLocation:
    if not ha_area_id:
        return DeviceLocation()
    room_name = (area_name or ha_area_id).strip()
    return DeviceLocation(
        provider="home_assistant",
        room_id=room_name.lower().replace(" ", "_"),
        room_name=room_name,
        ha_area_id=ha_area_id,
    )


def _pick_credential(credentials: list[DeviceCredentialSummary]) -> DeviceCredentialSummary | None:
    if not credentials:
        return None
    active = [c for c in credentials if c.revoked_at is None]
    pool = active or credentials
    return max(
        pool,
        key=lambda c: (
            c.last_seen_at or datetime.min.replace(tzinfo=timezone.utc),
            c.created_at,
        ),
    )


def _active_device_id(credentials: list[DeviceCredentialSummary]) -> str | None:
    active = [c for c in credentials if c.revoked_at is None]
    picked = _pick_credential(active)
    return picked.device_id if picked else None


def _status_sort_key(node: PresenceNode) -> tuple[int, str]:
    order = {"online": 0, "offline": 1}
    label = (node.node_label or node.node_id).casefold()
    return (order.get(node.status, 9), label)


async def build_presence_view(
    owner_id: str,
    *,
    manager: ConnectionManager | None = None,
) -> PresenceView:
    """Build owner-scoped presence: live sessions merged with provisioned credentials."""
    if manager is None:
        from api.websockets.connection import manager as default_manager

        manager = default_manager

    credentials = await device_auth_service.list_devices(owner_id=owner_id)
    creds_by_node: dict[str, list[DeviceCredentialSummary]] = {}
    for cred in credentials:
        creds_by_node.setdefault(cred.node_id, []).append(cred)

    live_by_node: dict[str, Session] = {}
    for session in manager.list_owner_sessions(owner_id):
        live_by_node[session.presence.node_id] = session

    live_endpoints = manager.list_live_endpoints(owner_id)
    active_endpoint = _pick_best(live_endpoints)
    active_node_id = active_endpoint.node_id if active_endpoint else None

    node_ids = set(creds_by_node) | set(live_by_node)
    nodes: list[PresenceNode] = []

    for node_id in node_ids:
        live = live_by_node.get(node_id)
        node_creds = creds_by_node.get(node_id, [])
        cred = _pick_credential(node_creds)
        all_revoked = bool(node_creds) and all(c.revoked_at is not None for c in node_creds)

        # Revoked credentials remain stored for auth rejection, not presence.
        if live is None and all_revoked:
            continue

        if live is not None:
            status = "online"
        else:
            status = "offline"

        kind: DeviceKind | Literal["unknown"] = (
            live.presence.device_kind
            if live is not None
            else (cred.kind if cred else "unknown")
        )
        device_id = _active_device_id(node_creds)
        if device_id is None and cred is not None:
            device_id = cred.device_id

        last_seen = cred.last_seen_at if cred else None
        room_name = _room_name_from_credential(cred) if cred else None
        ha_area_id = cred.location.ha_area_id if cred else None
        capabilities = list(cred.capabilities) if cred else []
        node_label = cred.node_label if cred else None

        if live is not None:
            node_label = live.presence.node_label or node_label
            capabilities = sorted(live.presence.capabilities)
            room_name = _room_name_from_session(live) or room_name
            ha_area_id = live.presence.location.ha_area_id or ha_area_id
            if last_seen is None:
                last_seen = _utcnow()

        nodes.append(
            PresenceNode(
                node_id=node_id,
                node_label=node_label,
                kind=kind,
                status=status,
                capabilities=capabilities,
                room_name=room_name,
                ha_area_id=ha_area_id,
                last_seen_at=last_seen,
                active=node_id == active_node_id,
                device_id=device_id,
            )
        )

    nodes.sort(key=_status_sort_key)

    return PresenceView(
        core=PresenceCore(name=settings.SYSTEM_NAME),
        nodes=nodes,
    )


async def assign_node_room(
    node_id: str,
    *,
    owner_id: str,
    ha_area_id: str | None,
    area_name: str | None,
    manager: ConnectionManager | None = None,
) -> PresenceView:
    """Persist endpoint room assignment and refresh live presence."""
    if manager is None:
        from api.websockets.connection import manager as default_manager

        manager = default_manager

    location = _location_for_ha_area(ha_area_id, area_name)
    modified = await device_auth_service.update_node_location(
        owner_id=owner_id,
        node_id=node_id,
        location=location,
    )
    if modified == 0:
        raise ValueError("Pair this endpoint before assigning it to a room.")

    manager.update_node_location(owner_id, node_id, location)
    await manager.broadcast_presence_changed(owner_id)
    return await build_presence_view(owner_id, manager=manager)


async def revoke_presence_device(
    device_id: str,
    *,
    owner_id: str,
    manager: ConnectionManager | None = None,
) -> bool:
    """Revoke a device credential and force-disconnect any live session for its node."""
    if manager is None:
        from api.websockets.connection import DEVICE_REVOKED_CLOSE_CODE, manager as default_manager

        manager = default_manager
    else:
        from api.websockets.connection import DEVICE_REVOKED_CLOSE_CODE

    devices = await device_auth_service.list_devices(owner_id=owner_id)
    target = next((d for d in devices if d.device_id == device_id), None)
    if target is None:
        return False

    revoked = await device_auth_service.revoke_device(device_id)
    if not revoked and target.revoked_at is not None:
        revoked = True

    if revoked:
        disconnected = False
        for session in manager.list_owner_sessions(owner_id):
            if session.presence.node_id == target.node_id:
                disconnected = True
                await manager.disconnect(
                    session.connection_id,
                    code=DEVICE_REVOKED_CLOSE_CODE,
                    reason="device_revoked",
                )
        if not disconnected:
            await manager.broadcast_presence_changed(owner_id)

    return revoked
