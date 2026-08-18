"""Home Assistant visibility REST API."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from api.deps.device_auth import require_device, require_owner_id
from api.oauth_support import (
    assert_allowed_oauth_origin,
    oauth_callback_response,
    publish_oauth_changed,
)
from plugins.smart_home.auth_flow import (
    APP_NAME as HA_AUTH_APP,
    complete_ha_auth_flow,
    consume_ha_auth_flow,
    issue_ha_auth_flow,
)
from plugins.smart_home.config import (
    clear_ha_connection,
    persist_ha_connection,
    resolve_ha_connection,
    validate_ha_connection,
)
from plugins.smart_home.discovery import discover_home_assistant
from plugins.smart_home.ha_client import HomeAssistantClient, HomeAssistantError
from plugins.smart_home.node_binding import ha_area_to_location
from plugins.smart_home.rooms import (
    RoomMutationResponse,
    RoomsResponse,
    create_room,
    delete_room,
    list_rooms,
    rename_room,
    unknown_location,
)
from plugins.smart_home.ui_status import SmartHomeStatusResponse, build_smart_home_status

router = APIRouter(prefix="/smart-home", tags=["smart-home"])
logger = logging.getLogger(__name__)


class RoomCreateRequest(BaseModel):
    name: str


class RoomRenameRequest(BaseModel):
    name: str


class HaConnectRequest(BaseModel):
    url: str = Field(min_length=1)
    token: str = Field(min_length=1)


class HaDiscoverResponse(BaseModel):
    found: bool
    url: str | None = None


class HaAuthorizeRequest(BaseModel):
    url: str = Field(min_length=1)
    origin: str = Field(min_length=1)


class HaAuthorizeResponse(BaseModel):
    authorize_url: str
    ha_url: str


async def _require_client() -> HomeAssistantClient:
    url, token = await resolve_ha_connection()
    if not url or not token:
        raise HTTPException(status_code=400, detail="Home Assistant is not connected.")
    return HomeAssistantClient(base_url=url, token=token)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, HomeAssistantError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/status", response_model=SmartHomeStatusResponse)
async def get_smart_home_status(_auth=Depends(require_device)) -> SmartHomeStatusResponse:
    return await build_smart_home_status()


@router.get("/discover", response_model=HaDiscoverResponse)
async def discover_smart_home(_auth=Depends(require_device)) -> HaDiscoverResponse:
    url = await discover_home_assistant()
    return HaDiscoverResponse(found=url is not None, url=url)


@router.post("/auth/authorize", response_model=HaAuthorizeResponse)
async def authorize_smart_home(
    request: HaAuthorizeRequest,
    http_request: Request,
    _auth=Depends(require_device),
) -> HaAuthorizeResponse:
    origin = assert_allowed_oauth_origin(
        request.origin,
        http_request.headers.get("origin"),
    )
    try:
        started = issue_ha_auth_flow(ha_url=request.url, origin=origin)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return HaAuthorizeResponse(authorize_url=started.authorize_url, ha_url=started.ha_url)


@router.get("/auth/callback", response_class=HTMLResponse)
async def smart_home_auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = Query(None, alias="error_description"),
) -> HTMLResponse:
    if error:
        await publish_oauth_changed(app=HA_AUTH_APP, success=False)
        return oauth_callback_response(
            title="Authorization Failed",
            message=f"Authorization failed: {error_description or error}",
            success=False,
            app_name=HA_AUTH_APP,
        )

    if not code or not state:
        await publish_oauth_changed(app=HA_AUTH_APP, success=False)
        return oauth_callback_response(
            title="Authorization Incomplete",
            message="The authorization was not completed. You can close this window.",
            success=False,
            app_name=HA_AUTH_APP,
        )

    flow = consume_ha_auth_flow(state)
    if not flow:
        await publish_oauth_changed(app=HA_AUTH_APP, success=False)
        return oauth_callback_response(
            title="Authorization Error",
            message="State validation failed. Please try again.",
            success=False,
            app_name=HA_AUTH_APP,
        )

    try:
        await complete_ha_auth_flow(code=code, flow=flow)
    except Exception:
        logger.exception("Home Assistant authorization callback failed")
        await publish_oauth_changed(app=HA_AUTH_APP, success=False)
        return oauth_callback_response(
            title="Authorization Failed",
            message="Could not finish connecting Home Assistant. Please try again.",
            success=False,
            app_name=HA_AUTH_APP,
        )

    await publish_oauth_changed(app=HA_AUTH_APP, success=True, loaded=True)
    return oauth_callback_response(
        title="Home Assistant Connected",
        message="Home Assistant is connected. You can close this window.",
        success=True,
        app_name=HA_AUTH_APP,
        loaded=True,
    )


@router.post("/connect", response_model=SmartHomeStatusResponse)
async def connect_smart_home(
    request: HaConnectRequest,
    _auth=Depends(require_device),
) -> SmartHomeStatusResponse:
    liveness = await validate_ha_connection(request.url, request.token)
    if not liveness.authenticated:
        raise HTTPException(status_code=400, detail=liveness.message)
    await persist_ha_connection(request.url, request.token)
    return await build_smart_home_status()


@router.delete("/connect", response_model=SmartHomeStatusResponse)
async def disconnect_smart_home(_auth=Depends(require_device)) -> SmartHomeStatusResponse:
    await clear_ha_connection()
    return await build_smart_home_status()


@router.get("/rooms", response_model=RoomsResponse)
async def get_rooms(owner_id: str = Depends(require_owner_id)) -> RoomsResponse:
    client = await _require_client()
    try:
        return await list_rooms(client, owner_id=owner_id)
    except Exception as exc:
        raise _http_error(exc) from exc
    finally:
        await client.aclose()


@router.post("/rooms", response_model=RoomMutationResponse)
async def post_room(
    request: RoomCreateRequest,
    owner_id: str = Depends(require_owner_id),
) -> RoomMutationResponse:
    client = await _require_client()
    try:
        return await create_room(client, owner_id=owner_id, name=request.name)
    except Exception as exc:
        raise _http_error(exc) from exc
    finally:
        await client.aclose()


@router.patch("/rooms/{area_id}", response_model=RoomMutationResponse)
async def patch_room(
    area_id: str,
    request: RoomRenameRequest,
    owner_id: str = Depends(require_owner_id),
) -> RoomMutationResponse:
    client = await _require_client()
    try:
        result = await rename_room(
            client,
            owner_id=owner_id,
            area_id=area_id,
            name=request.name,
        )
        from api.websockets.connection import manager as connection_manager

        for node_id in result.affected_node_ids:
            connection_manager.update_node_location(
                owner_id,
                node_id,
                ha_area_to_location(area_id, result.room.name if result.room else request.name),
            )
        return result
    except Exception as exc:
        raise _http_error(exc) from exc
    finally:
        await client.aclose()


@router.delete("/rooms/{area_id}", response_model=RoomMutationResponse)
async def delete_room_route(
    area_id: str,
    owner_id: str = Depends(require_owner_id),
) -> RoomMutationResponse:
    client = await _require_client()
    try:
        result = await delete_room(client, owner_id=owner_id, area_id=area_id)
        from api.websockets.connection import manager as connection_manager

        for node_id in result.cleared_node_ids:
            connection_manager.update_node_location(
                owner_id,
                node_id,
                unknown_location(),
            )
        return result
    except Exception as exc:
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
