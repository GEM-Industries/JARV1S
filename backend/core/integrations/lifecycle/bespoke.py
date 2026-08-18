"""
Lifecycle ops that don't go through Composio: tearing down a locally-loaded
plugin, and refreshing non-Composio MCP bridges from live schemas.
"""

from __future__ import annotations

import logging

from core.config import settings
from core.integrations.lifecycle._shared import deregister_local
from core.integrations.mcp.bridge import load_mcp_bridges
from core.integrations.mcp.cache import invalidate_cache
from core.integrations.mcp.config import load_mcp_config
from core.plugins.registry import registry
from core.tool_router import tool_router

logger = logging.getLogger(__name__)


async def teardown_local_integration(name: str) -> bool:
    """Remove a locally mounted integration plugin and its routed tools."""
    return await deregister_local(name)


async def refresh_non_composio_integrations() -> list[str]:
    """Reload non-Composio MCP bridge integrations from live schemas."""
    if not settings.MCP_SERVERS_CONFIG or not settings.MCP_SERVERS_CONFIG.exists():
        return []

    configs = load_mcp_config(settings.MCP_SERVERS_CONFIG)
    refreshed_names: list[str] = []
    for config in configs:
        if config.type == "composio":
            continue
        refreshed_names.append(config.name)
        invalidate_cache(config.name)
        await teardown_local_integration(config.name)

    await load_mcp_bridges()

    for name in refreshed_names:
        if name not in registry.plugins:
            continue
        plugin = registry.plugins.get(name)
        if plugin:
            utterances = plugin.metadata.utterances
            await tool_router.register_plugin(name, plugin.get_tools(), utterances=utterances or None)

    return refreshed_names
