"""UI-oriented Home Assistant status for REST surfaces."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from plugins.smart_home.config import resolve_ha_connection
from plugins.smart_home.domains import is_safe_setup_entity
from plugins.smart_home.ha_client import HomeAssistantClient, normalize_ha_url
from plugins.smart_home.inventory import InventorySnapshot, entity_to_device_summary
from plugins.smart_home.models import DeviceSummary
from plugins.smart_home.status import check_liveness, check_readiness

MAX_UI_DEVICES = 7


class SmartHomeUiStatus(str, Enum):
    UNCONFIGURED = "unconfigured"
    INVALID_CONFIG = "invalid_config"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    REGISTRY_UNAVAILABLE = "registry_unavailable"
    EMPTY_INVENTORY = "empty_inventory"
    READY = "ready"


class SmartHomeStatusResponse(BaseModel):
    status: SmartHomeUiStatus
    message: str
    next_action: str | None = None
    # Normalized HA base URL (scheme://host), safe to link to. Never carries a token.
    ha_url: str | None = None
    configured: bool = False
    reachable: bool = False
    authenticated: bool = False
    registry_access: bool = False
    ready: bool = False
    area_count: int = 0
    device_count: int = 0
    safe_controllable_count: int = 0
    devices: list[DeviceSummary] = Field(default_factory=list)
    devices_truncated: bool = False


def _safe_device_summaries(snapshot: InventorySnapshot) -> tuple[list[DeviceSummary], bool]:
    safe_entities = [e for e in snapshot.entities if is_safe_setup_entity(e.entity_id)]
    safe_entities.sort(key=lambda e: (e.name.casefold(), e.entity_id))
    truncated = len(safe_entities) > MAX_UI_DEVICES
    summaries = [entity_to_device_summary(e) for e in safe_entities[:MAX_UI_DEVICES]]
    return summaries, truncated


async def build_smart_home_status() -> SmartHomeStatusResponse:
    """Map known Home Assistant states into a single stable UI contract."""
    url, token = await resolve_ha_connection()

    if not url or not token:
        return SmartHomeStatusResponse(
            status=SmartHomeUiStatus.UNCONFIGURED,
            message="Home Assistant is not connected yet.",
            next_action="JARV1S will look for Home Assistant, then ask you to sign in.",
        )

    try:
        ha_url = normalize_ha_url(url)
    except ValueError as e:
        return SmartHomeStatusResponse(
            status=SmartHomeUiStatus.INVALID_CONFIG,
            message=str(e),
            next_action="Check the URL below and try again.",
            configured=True,
        )

    liveness = await check_liveness(url, token)
    base = {
        "ha_url": ha_url,
        "configured": liveness.configured,
        "reachable": liveness.reachable,
        "authenticated": liveness.authenticated,
    }

    if not liveness.reachable:
        return SmartHomeStatusResponse(
            status=SmartHomeUiStatus.UNREACHABLE,
            message=liveness.message,
            next_action="Start Home Assistant, then tap Refresh.",
            **base,
        )

    if not liveness.authenticated:
        return SmartHomeStatusResponse(
            status=SmartHomeUiStatus.AUTH_FAILED,
            message=liveness.message,
            next_action="Sign in again to reconnect. Manual token connect remains available below.",
            **base,
        )

    client = HomeAssistantClient(base_url=url, token=token)
    try:
        readiness = await check_readiness(client)
    finally:
        await client.aclose()

    counts = {
        **base,
        "registry_access": readiness.registry_access,
        "ready": readiness.ready,
        "area_count": readiness.area_count,
        "device_count": readiness.device_count,
        "safe_controllable_count": readiness.safe_controllable_count,
    }

    if not readiness.registry_access:
        return SmartHomeStatusResponse(
            status=SmartHomeUiStatus.REGISTRY_UNAVAILABLE,
            message=readiness.message,
            next_action="Open Home Assistant to check it finished starting, then Refresh.",
            **counts,
        )

    devices, truncated = (
        _safe_device_summaries(readiness.snapshot)
        if readiness.snapshot is not None
        else ([], False)
    )

    if readiness.safe_controllable_count == 0:
        return SmartHomeStatusResponse(
            status=SmartHomeUiStatus.EMPTY_INVENTORY,
            message="Connected to Home Assistant, but no controllable devices yet.",
            next_action="Add your first device in Home Assistant, then Refresh.",
            devices=devices,
            devices_truncated=truncated,
            **counts,
        )

    return SmartHomeStatusResponse(
        status=SmartHomeUiStatus.READY,
        message=readiness.message,
        devices=devices,
        devices_truncated=truncated,
        **counts,
    )
