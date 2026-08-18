"""Smart Home integration status helpers."""

from __future__ import annotations

from pydantic import BaseModel, Field

from plugins.smart_home.domains import is_safe_setup_entity
from plugins.smart_home.ha_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
    HomeAssistantError,
)
from plugins.smart_home.inventory import (
    CACHE_TTL_S,
    InventorySnapshot,
    build_inventory,
    find_safe_setup_candidate,
)


class LivenessStatus(BaseModel):
    configured: bool
    reachable: bool
    authenticated: bool
    message: str


class ReadinessStatus(BaseModel):
    liveness: LivenessStatus
    registry_access: bool
    entity_count: int
    safe_controllable_count: int
    area_count: int = 0
    device_count: int = 0
    setup_candidate: str | None = None
    ready: bool
    message: str
    snapshot: InventorySnapshot | None = Field(default=None, exclude=True)


def _missing_config() -> LivenessStatus:
    return LivenessStatus(
        configured=False,
        reachable=False,
        authenticated=False,
        message="Home Assistant is not configured. Connect it in the Smart Home panel.",
    )


async def check_liveness(url: str | None, token: str | None) -> LivenessStatus:
    if not url or not token:
        return _missing_config()

    try:
        client = HomeAssistantClient(base_url=url, token=token)
    except ValueError as e:
        return LivenessStatus(
            configured=True,
            reachable=False,
            authenticated=False,
            message=str(e),
        )

    try:
        await client.ping()
        return LivenessStatus(
            configured=True,
            reachable=True,
            authenticated=True,
            message="Home Assistant is reachable and the token is valid.",
        )
    except HomeAssistantAuthError as e:
        return LivenessStatus(
            configured=True,
            reachable=True,
            authenticated=False,
            message=str(e),
        )
    except HomeAssistantConnectionError as e:
        return LivenessStatus(
            configured=True,
            reachable=False,
            authenticated=False,
            message=str(e),
        )
    except HomeAssistantError as e:
        return LivenessStatus(
            configured=True,
            reachable=False,
            authenticated=False,
            message=str(e),
        )
    finally:
        await client.aclose()


async def check_readiness(client: HomeAssistantClient) -> ReadinessStatus:
    liveness = await check_liveness(client.base_url, client.token)
    if not liveness.authenticated:
        return ReadinessStatus(
            liveness=liveness,
            registry_access=False,
            entity_count=0,
            safe_controllable_count=0,
            ready=False,
            message=liveness.message,
        )

    try:
        snapshot = await build_inventory(client)
    except HomeAssistantError as e:
        return ReadinessStatus(
            liveness=liveness,
            registry_access=False,
            entity_count=0,
            safe_controllable_count=0,
            ready=False,
            message=str(e),
        )

    safe_count = sum(1 for e in snapshot.entities if is_safe_setup_entity(e.entity_id))
    candidate = find_safe_setup_candidate(snapshot)
    ready = snapshot.entity_count > 0 and safe_count > 0
    message = (
        "Home Assistant is ready for device control."
        if ready
        else "Home Assistant is connected but no safe controllable devices were found."
    )
    return ReadinessStatus(
        liveness=liveness,
        registry_access=True,
        entity_count=snapshot.entity_count,
        safe_controllable_count=safe_count,
        area_count=snapshot.area_count,
        device_count=snapshot.device_count,
        setup_candidate=candidate.entity_id if candidate else None,
        ready=ready,
        message=message,
        snapshot=snapshot,
    )


async def load_or_refresh_inventory(client: HomeAssistantClient, cached: dict | None) -> InventorySnapshot:
    import time

    if cached:
        captured = cached.get("captured_at_epoch", 0)
        if time.time() - captured < CACHE_TTL_S:
            return InventorySnapshot.model_validate(cached["snapshot"])

    snapshot = await build_inventory(client)
    return snapshot


async def force_refresh_inventory(client: HomeAssistantClient) -> InventorySnapshot:
    """Bypass cache and rebuild inventory from Home Assistant."""
    return await build_inventory(client)


def inventory_cache_payload(snapshot: InventorySnapshot) -> dict:
    import time

    return {
        "captured_at_epoch": time.time(),
        "snapshot": snapshot.model_dump(mode="json"),
    }
