"""
Streamable HTTP MCP Client.

Implements the same interface as MCPClient (start, list_tools, call_tool, shutdown)
but over the MCP Streamable HTTP transport instead of stdio.

Protocol:
  - Client-to-server: HTTP POST with JSON-RPC body.
  - Server-to-client: optional SSE stream at the same URL (GET).
  - Session management: server assigns Mcp-Session-Id on initialize; client
    echoes it on all subsequent requests.
  - Connection lifecycle managed via HTTP semantics, not process lifetime.

References:
  https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http
"""

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

from core.integrations.mcp.client import MCPError, normalize_tool_result

logger = logging.getLogger(__name__)

RESPONSE_TIMEOUT = 30.0


class MCPHTTPError(MCPError):
    """Raised when the remote MCP server returns an HTTP-level or RPC error."""
    pass


class StreamableHTTPClient:
    """
    Async Streamable HTTP MCP client.

    Usage:
        client = StreamableHTTPClient(url="https://mcp.example.com/server")
        await client.start()
        tools = await client.list_tools()
        result = await client.call_tool("search", {"query": "hello"})
        await client.shutdown()

    The interface is identical to MCPClient so MCPBridgePlugin works with
    either transport without modification.
    """

    def __init__(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = RESPONSE_TIMEOUT,
    ) -> None:
        self._url = url.rstrip("/")
        self._extra_headers = headers or {}
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._request_id = 0
        self._session_id: Optional[str] = None
        self._initialized = False

    async def start(self) -> None:
        """Create the HTTP client and perform the MCP initialize handshake."""
        self._client = httpx.AsyncClient(
            headers=self._build_headers(),
            timeout=self._timeout,
            follow_redirects=True,
        )
        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "jarvis", "version": "1.0"},
        })
        await self._notify("notifications/initialized", {})
        self._initialized = True
        logger.info("Streamable HTTP MCP client connected: %s", self._url)

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Return the list of tools available from this MCP server."""
        result = await self._request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the MCP server. Raises MCPHTTPError on server error."""
        result = await self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return normalize_tool_result(result, tool_name, MCPHTTPError)

    async def shutdown(self) -> None:
        """Close the underlying HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
            self._session_id = None
            self._initialized = False
            logger.info("Streamable HTTP MCP client disconnected: %s", self._url)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _build_headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._extra_headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _request(self, method: str, params: Dict[str, Any]) -> Any:
        """Send a JSON-RPC request via HTTP POST and return the result."""
        if not self._client:
            raise RuntimeError("StreamableHTTPClient is not started.")

        req_id = self._next_id()
        body = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        try:
            response = await self._client.post(
                self._url, json=body, headers=self._build_headers()
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise MCPHTTPError(
                f"MCP HTTP error {e.response.status_code} for {method}: {e.response.text}"
            ) from e
        except httpx.RequestError as e:
            raise MCPHTTPError(f"MCP HTTP request failed for {method}: {e}") from e

        # Capture session ID from server (assigned on initialize response)
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            msg = _parse_sse_first_event(response.text)
        else:
            msg = response.json()

        if "error" in msg:
            raise MCPHTTPError(f"MCP RPC error ({method}): {msg['error']}")

        return msg.get("result", {})

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if not self._client:
            return
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            await self._client.post(
                self._url, json=body, headers=self._build_headers()
            )
        except Exception as e:
            logger.debug("MCP HTTP notification '%s' failed (non-fatal): %s", method, e)


def _parse_sse_first_event(text: str) -> Dict[str, Any]:
    """Extract the JSON payload from the first SSE data line."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data = line[5:].strip()
            if data:
                return json.loads(data)
    raise MCPHTTPError("No data event found in SSE response")
