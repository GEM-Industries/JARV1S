"""Room read model backed by Home Assistant areas."""

from __future__ import annotations

from pydantic import BaseModel, Field

from core.auth.device_models import DeviceKind, DeviceLocation
from core.auth.device_service import device_auth_service
from services.database.mongodb import mongodb
from plugins.smart_home.ha_client import HomeAssistantClient
from plugins.smart_home.inventory import TOOL_DATA_KEY, build_inventory


class BoundRoomNode(BaseModel):
    node_id: str
    node_label: str | None = None
    device_id: str | None = None
    kind: DeviceKind
    room_name: str | None = None


class RoomSummary(BaseModel):
    area_id: str
    name: str
    exists_in_ha: bool = True
    device_count: int = 0
    entity_count: int = 0
    bound_nodes: list[BoundRoomNode] = Field(default_factory=list)


class RoomsResponse(BaseModel):
    rooms: list[RoomSummary] = Field(default_factory=list)


class RoomMutationResponse(BaseModel):
    room: RoomSummary | None = None
    rooms: list[RoomSummary] = Field(default_factory=list)
    affected_node_ids: list[str] = Field(default_factory=list)
    cleared_node_ids: list[str] = Field(default_factory=list)


def _area_name(area: dict) -> str:
    return str(area.get("name") or area.get("area_id") or "Unnamed")


def _normalize_room_name(name: str) -> str:
    return " ".join(name.strip().split())


async def invalidate_inventory_cache(owner_id: str) -> None:
    data = await mongodb.get_tool_data(owner_id, "smart_home")
    data.pop(TOOL_DATA_KEY, None)
    await mongodb.store_tool_data(owner_id, "smart_home", data)


async def list_rooms(
    client: HomeAssistantClient,
    *,
    owner_id: str,
) -> RoomsResponse:
    """Build rooms from HA areas, inventory counts, and JARV1S node bindings."""
    areas = await client.list_areas()
    snapshot = await build_inventory(client)
    area_ids = {str(area["area_id"]) for area in areas if area.get("area_id")}
    rooms: dict[str, RoomSummary] = {
        str(area["area_id"]): RoomSummary(
            area_id=str(area["area_id"]),
            name=_area_name(area),
        )
        for area in areas
        if area.get("area_id")
    }

    device_ids_by_area: dict[str, set[str]] = {}
    for entity in snapshot.entities:
        if not entity.area_id:
            continue
        room = rooms.get(entity.area_id)
        if room is None:
            continue
        room.entity_count += 1
        device_key = entity.device_id or entity.entity_id
        device_ids_by_area.setdefault(entity.area_id, set()).add(device_key)

    for area_id, device_ids in device_ids_by_area.items():
        if area_id in rooms:
            rooms[area_id].device_count = len(device_ids)

    credentials = await device_auth_service.list_devices(owner_id=owner_id)
    for cred in credentials:
        if cred.revoked_at is not None:
            continue
        location = cred.location
        if not location.ha_area_id:
            continue
        area_id = location.ha_area_id
        if area_id not in rooms:
            rooms[area_id] = RoomSummary(
                area_id=area_id,
                name=location.room_name or location.room_id or area_id,
                exists_in_ha=area_id in area_ids,
            )
        rooms[area_id].bound_nodes.append(
            BoundRoomNode(
                node_id=cred.node_id,
                node_label=cred.node_label,
                device_id=cred.device_id,
                kind=cred.kind,
                room_name=location.room_name,
            )
        )

    return RoomsResponse(
        rooms=sorted(
            rooms.values(),
            key=lambda room: (not room.exists_in_ha, room.name.casefold(), room.area_id),
        )
    )


async def create_room(
    client: HomeAssistantClient,
    *,
    owner_id: str,
    name: str,
) -> RoomMutationResponse:
    room_name = _normalize_room_name(name)
    if not room_name:
        raise ValueError("Room name is required")
    created = await client.create_area(room_name)
    await invalidate_inventory_cache(owner_id)
    rooms = await list_rooms(client, owner_id=owner_id)
    area_id = str(created.get("area_id") or "")
    room = next((item for item in rooms.rooms if item.area_id == area_id), None)
    return RoomMutationResponse(room=room, rooms=rooms.rooms)


async def rename_room(
    client: HomeAssistantClient,
    *,
    owner_id: str,
    area_id: str,
    name: str,
) -> RoomMutationResponse:
    room_name = _normalize_room_name(name)
    if not room_name:
        raise ValueError("Room name is required")
    updated = await client.update_area(area_id, name=room_name)
    await invalidate_inventory_cache(owner_id)
    changed_nodes = await device_auth_service.update_area_room_name(
        owner_id=owner_id,
        ha_area_id=area_id,
        room_name=str(updated.get("name") or room_name),
    )
    rooms = await list_rooms(client, owner_id=owner_id)
    room = next((item for item in rooms.rooms if item.area_id == area_id), None)
    return RoomMutationResponse(room=room, rooms=rooms.rooms, affected_node_ids=changed_nodes)


async def delete_room(
    client: HomeAssistantClient,
    *,
    owner_id: str,
    area_id: str,
) -> RoomMutationResponse:
    await client.delete_area(area_id)
    await invalidate_inventory_cache(owner_id)
    cleared_nodes = await device_auth_service.clear_area_location(
        owner_id=owner_id,
        ha_area_id=area_id,
    )
    rooms = await list_rooms(client, owner_id=owner_id)
    return RoomMutationResponse(
        rooms=rooms.rooms,
        affected_node_ids=cleared_nodes,
        cleared_node_ids=cleared_nodes,
    )


def unknown_location() -> DeviceLocation:
    return DeviceLocation()
