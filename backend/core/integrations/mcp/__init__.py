"""
MCP (Model Context Protocol) integration layer.

Provides stdio and HTTP transports, auto-bridge from MCP tool schemas to
JarvisPlugin instances, config parsing, and schema caching.
"""

from core.integrations.mcp.client import (
    MCPClient,
    MCPError,
    normalize_tool_result,
    shutdown_all_mcp_clients,
)
from core.integrations.mcp.http_client import StreamableHTTPClient, MCPHTTPError
from core.integrations.mcp.bridge import MCPBridgePlugin, load_mcp_bridges

__all__ = [
    "MCPClient",
    "MCPError",
    "normalize_tool_result",
    "shutdown_all_mcp_clients",
    "StreamableHTTPClient",
    "MCPHTTPError",
    "MCPBridgePlugin",
    "load_mcp_bridges",
]
