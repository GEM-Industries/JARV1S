"""Presence visibility REST API."""

from fastapi import APIRouter, Depends, HTTPException

from api.deps.device_auth import require_owner_id
from core.presence.models import AssignNodeRoomRequest, PresenceView, RevokeDeviceResponse
from core.presence.service import assign_node_room, build_presence_view, revoke_presence_device
from plugins.smart_home.config import resolve_ha_connection
from plugins.smart_home.ha_client import HomeAssistantClient, HomeAssistantError

router = APIRouter(prefix="/presence", tags=["presence"])


async def _resolve_ha_area_name(area_id: str) -> str:
    url, token = await resolve_ha_connection()
    if not url or not token:
        raise HTTPException(status_code=400, detail="Home Assistant is not connected.")
    client = HomeAssistantClient(base_url=url, token=token)
    try:
        areas = await client.list_areas()
    except HomeAssistantError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await client.aclose()

    area = next((item for item in areas if str(item.get("area_id") or "") == area_id), None)
    if area is None:
        raise HTTPException(status_code=404, detail="Home Assistant room not found.")
    return str(area.get("name") or area_id)


@router.get("/", response_model=PresenceView)
async def get_presence(owner_id: str = Depends(require_owner_id)) -> PresenceView:
    return await build_presence_view(owner_id)


@router.post("/devices/{device_id}/revoke", response_model=RevokeDeviceResponse)
async def revoke_device(
    device_id: str,
    owner_id: str = Depends(require_owner_id),
) -> RevokeDeviceResponse:
    revoked = await revoke_presence_device(device_id, owner_id=owner_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Device not found or already revoked")
    return RevokeDeviceResponse(revoked=True)


@router.patch("/nodes/{node_id}/room", response_model=PresenceView)
async def patch_node_room(
    node_id: str,
    request: AssignNodeRoomRequest,
    owner_id: str = Depends(require_owner_id),
) -> PresenceView:
    area_name = await _resolve_ha_area_name(request.ha_area_id) if request.ha_area_id else None

    try:
        return await assign_node_room(
            node_id,
            owner_id=owner_id,
            ha_area_id=request.ha_area_id,
            area_name=area_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
