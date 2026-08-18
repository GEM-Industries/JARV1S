"""Home Assistant client factory."""

from __future__ import annotations

from typing import Any

from plugins.smart_home.ha_client import HomeAssistantClient, create_ha_client

__all__ = ["create_smart_home_client", "HomeAssistantClient"]


async def create_smart_home_client(config: dict[str, Any]) -> HomeAssistantClient:
    return await create_ha_client(config)
