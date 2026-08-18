"""Node location binding via device credentials."""

from __future__ import annotations

from core.auth.device_models import DeviceLocation
from core.auth.device_service import device_auth_service
from core.context import get_owner_id


def ha_area_to_location(ha_area_id: str, area_name: str | None) -> DeviceLocation:
    room_slug = (area_name or ha_area_id).strip().lower().replace(" ", "_")
    return DeviceLocation(
        provider="home_assistant",
        room_id=room_slug,
        room_name=area_name,
        ha_area_id=ha_area_id,
    )


async def bind_node(
    owner_id: str,
    node_id: str,
    ha_area_id: str,
    *,
    area_name: str | None = None,
) -> DeviceLocation:
    location = ha_area_to_location(ha_area_id, area_name)
    modified = await device_auth_service.update_node_location(
        owner_id=owner_id,
        node_id=node_id,
        location=location,
    )
    if modified == 0:
        raise RuntimeError(f"No device credential found for node {node_id!r}.")
    return location


async def resolve_area_for_node(node_id: str | None, *, owner_id: str | None = None) -> str | None:
    if not node_id:
        return None
    oid = owner_id or get_owner_id()
    location = await device_auth_service.get_node_location(owner_id=oid, node_id=node_id)
    return location.ha_area_id if location else None


async def resolve_area_from_context(
    location_ref: dict | None,
    node_id: str | None,
    *,
    owner_id: str | None = None,
) -> str | None:
    if location_ref and location_ref.get("ha_area_id"):
        return str(location_ref["ha_area_id"])
    return await resolve_area_for_node(node_id, owner_id=owner_id)


async def resolve_location_ref_for_area_name(
    owner_id: str,
    area_name: str,
) -> dict[str, str | None] | None:
    return await device_auth_service.resolve_location_ref_for_area_name(
        owner_id=owner_id,
        area_name=area_name,
    )


async def list_bound_room_names(owner_id: str) -> list[str]:
    return await device_auth_service.list_bound_room_names(owner_id=owner_id)
