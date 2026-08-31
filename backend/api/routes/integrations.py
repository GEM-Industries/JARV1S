"""Integrations management REST API."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps.device_auth import require_device
from core.integrations.composio_gateway import get_composio_gateway
from core.integrations.lifecycle import (
    IntegrationConflictError,
    IntegrationLifecycleError,
    IntegrationOperationError,
    IntegrationUnavailableError,
    IntegrationView,
    create_connect_link,
    disconnect_integration as disconnect_integration_lifecycle,
    get_integration as get_integration_lifecycle,
    is_toggleable,
    list_integrations as list_integrations_lifecycle,
    reconcile_integration,
    refresh_non_composio_integrations,
)
from core.plugins.registry import registry

router = APIRouter(prefix="/integrations", dependencies=[Depends(require_device)])


class ToggleBody(BaseModel):
    enabled: bool


class IntegrationList(BaseModel):
    items: list[IntegrationView]


class ConnectLinkResponse(BaseModel):
    name: str
    connect_url: str


class ActionResult(BaseModel):
    success: bool
    message: str


class CatalogItem(BaseModel):
    slug: str
    display_name: str
    description: str
    auth_type: str
    connected: bool = False
    managed_auth: bool = True


class CatalogList(BaseModel):
    items: list[CatalogItem]


def _raise_http(exc: IntegrationLifecycleError) -> None:
    if isinstance(exc, IntegrationUnavailableError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, IntegrationConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, IntegrationOperationError):
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail="Unexpected integration error.") from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", response_model=IntegrationList)
async def list_integrations() -> IntegrationList:
    return IntegrationList(items=await list_integrations_lifecycle())


@router.get("/catalog", response_model=CatalogList)
async def search_catalog(q: str = Query(default="", alias="q")) -> CatalogList:
    """Search Composio toolkit catalog. Returns available toolkits matching query.

    Marks toolkits as connected if the user has an active account for them.
    """
    gateway = get_composio_gateway()
    if not gateway:
        raise HTTPException(status_code=503, detail="Composio is not configured on this instance.")

    try:
        toolkits = await gateway.search_toolkits(q)
        connected_apps = set(await gateway.list_connected_apps())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch catalog: {e}") from e

    return CatalogList(
        items=[
            CatalogItem(
                slug=t["slug"],
                display_name=t["display_name"],
                description=t["description"],
                auth_type=t["auth_type"],
                connected=t["slug"] in connected_apps,
                managed_auth=t.get("managed_auth", True),
            )
            for t in toolkits
        ]
    )


@router.post("/calendar/macos", response_model=ActionResult)
async def authorize_macos_calendar_route() -> ActionResult:
    from plugins.calendar.providers.eventkit import (
        authorize_macos_calendar,
        host_calendar_configured,
        macos_calendar_message,
    )

    if not host_calendar_configured():
        raise HTTPException(status_code=400, detail="Calendar on this Mac is not available.")
    try:
        status = await authorize_macos_calendar()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return ActionResult(
        success=status == "authorized",
        message=macos_calendar_message(status),
    )


@router.get("/{name}", response_model=IntegrationView)
async def get_integration(name: str) -> IntegrationView:
    item = await get_integration_lifecycle(name)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Integration '{name}' not found.")
    return item


@router.post("/{name}/connect-link", response_model=ConnectLinkResponse)
async def get_connect_link(name: str) -> ConnectLinkResponse:
    try:
        connect_url = await create_connect_link(name)
    except IntegrationLifecycleError as exc:
        _raise_http(exc)
    return ConnectLinkResponse(name=name, connect_url=connect_url)


@router.delete("/{name}", response_model=ActionResult)
async def disconnect_integration(name: str) -> ActionResult:
    try:
        result = await disconnect_integration_lifecycle(name)
    except IntegrationLifecycleError as exc:
        _raise_http(exc)

    if result.remote_disconnected and not result.local_deregistered:
        return ActionResult(
            success=True,
            message=f"{name.title()} auth disconnected. Reconnect to restore access.",
        )
    if result.remote_disconnected:
        return ActionResult(
            success=True,
            message=f"{name.title()} disconnected. Its tools are no longer available.",
        )
    return ActionResult(
        success=True,
        message=f"{name.title()} was already disconnected remotely and has been removed from the local registry.",
    )


@router.post("/refresh", response_model=ActionResult)
async def refresh_integrations() -> ActionResult:
    refreshed_names = await refresh_non_composio_integrations()
    count = len(refreshed_names)
    return ActionResult(
        success=True,
        message=f"Refreshed {count} non-Composio integration{'s' if count != 1 else ''}.",
    )


@router.post("/{name}/reconcile", response_model=ActionResult)
async def reconcile_integration_route(name: str) -> ActionResult:
    try:
        result = await reconcile_integration(name)
    except IntegrationLifecycleError as exc:
        _raise_http(exc)

    if not result.connected:
        raise HTTPException(status_code=409, detail=result.message)

    return ActionResult(success=result.loaded, message=result.message)


@router.patch("/{name}/toggle", response_model=ActionResult)
async def toggle_plugin(name: str, body: ToggleBody) -> ActionResult:
    """Enable or disable a built-in plugin. Not applicable to Composio integrations."""
    if name not in registry.plugins:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found.")
    if not is_toggleable(name):
        raise HTTPException(status_code=400, detail=f"Plugin '{name}' cannot be toggled.")
    await registry.set_plugin_enabled(name, body.enabled)
    state = "enabled" if body.enabled else "disabled"
    return ActionResult(success=True, message=f"{name.title()} {state}.")
