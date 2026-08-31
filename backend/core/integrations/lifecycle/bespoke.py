"""
Lifecycle ops that don't go through Composio: tearing down a locally-loaded
plugin, and refreshing non-Composio MCP bridges from live schemas.
"""

from __future__ import annotations

import logging

from core.integrations.lifecycle._shared import deregister_local
from core.integrations.mcp.bridge import (
    live_bridge_names,
    load_mcp_bridges,
)
from core.integrations.mcp.cache import invalidate_cache
from core.integrations.mcp.config import MCPConfigError, load_runtime_mcp_servers
from core.plugins.registry import registry
from core.tool_router import tool_router

logger = logging.getLogger(__name__)


async def teardown_local_integration(name: str) -> bool:
    """Remove a locally mounted integration plugin and its routed tools."""
    return await deregister_local(name)


async def refresh_non_composio_integrations() -> list[str]:
    """Reload non-Composio MCP bridges from packaged + home JSON.

    Validates the complete candidate first. Invalid config keeps the current
    servers. Removed servers are torn down before the new set is registered.
    """
    try:
        candidate = load_runtime_mcp_servers()
    except MCPConfigError as exc:
        logger.error("MCP refresh rejected; keeping live servers: %s", exc)
        return []

    for name in live_bridge_names():
        invalidate_cache(name)
        await teardown_local_integration(name)

    loaded = await load_mcp_bridges(candidate)

    for name in loaded:
        plugin = registry.plugins.get(name)
        if plugin:
            utterances = plugin.metadata.utterances
            await tool_router.register_plugin(
                name, plugin.get_tools(), utterances=utterances or None
            )

    return loaded
