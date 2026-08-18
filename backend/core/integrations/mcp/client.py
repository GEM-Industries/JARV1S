"""
Lightweight stdio MCP Client.

Spawns a local MCP server subprocess, communicates via JSON-RPC over stdin/stdout.
Implements only the methods needed: initialize, tools/list, tools/call.

Process lifecycle is managed here:
- Created on first use (lazy)
- Terminated cleanly on application shutdown via shutdown()
- Zombie prevention: tracked globally so FastAPI lifespan can clean up all instances
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_live_clients: list["MCPClient"] = []

RESPONSE_TIMEOUT = 30.0


class MCPError(Exception):
    """Raised when the MCP server returns an error response."""
    pass


def normalize_tool_result(result: Dict[str, Any], tool_name: str, error_cls: type = MCPError) -> Any:
    """Shared response normalization for MCP tool call results.

    Single TextContent → parsed JSON or str, multi-content → list, isError → raise.
    Used by both MCPClient (stdio) and StreamableHTTPClient (HTTP).
    """
    if result.get("isError"):
        content = result.get("content", [{}])
        error_text = content[0].get("text", "Unknown MCP error") if content else "Unknown MCP error"
        raise error_cls(f"MCP tool '{tool_name}' error: {error_text}")

    content = result.get("content", [])
    if len(content) == 1 and content[0].get("type") == "text":
        text = content[0]["text"]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return text
    return content


class MCPClient:
    """
    Async stdio MCP client.

    Usage:
        client = MCPClient(server_command=["npx", "@smithery/ha-mcp"], env={"HA_TOKEN": ...})
        await client.start()
        tools = await client.list_tools()
        result = await client.call_tool("get_states", {})
        await client.shutdown()
    """

    def __init__(self, server_command: List[str], env: Optional[Dict[str, str]] = None):
        self._command = server_command
        self._env = env
        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_id = 0
        self._initialized = False
        self._stderr_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Spawn the MCP server subprocess and perform the JSON-RPC initialize handshake."""
        import os
        proc_env = {**os.environ, **(self._env or {})}

        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )
        _live_clients.append(self)
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        logger.info("MCP server started: %s (pid=%d)", self._command[0], self._process.pid)

        await self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "jarvis", "version": "1.0"},
        })
        await self._notify("notifications/initialized", {})
        self._initialized = True

    async def list_tools(self) -> List[Dict[str, Any]]:
        """Return the list of tools available from this MCP server."""
        result = await self._request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a tool on the MCP server. Raises MCPError on server error."""
        result = await self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return normalize_tool_result(result, tool_name, MCPError)

    async def shutdown(self) -> None:
        """Terminate the MCP server subprocess cleanly."""
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                self._process.kill()
            logger.info("MCP server stopped: %s", self._command[0])
        if self in _live_clients:
            _live_clients.remove(self)
        self._process = None
        self._initialized = False

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _drain_stderr(self) -> None:
        """Read and log stderr so the pipe buffer never fills and deadlocks the server."""
        try:
            while self._process and self._process.stderr:
                line = await self._process.stderr.readline()
                if not line:
                    break
                logger.debug("MCP stderr [%s]: %s", self._command[0], line.decode().rstrip())
        except asyncio.CancelledError:
            pass

    async def _write(self, message: Dict[str, Any]) -> None:
        """Write a newline-delimited JSON-RPC message to the server's stdin."""
        if not self._process or not self._process.stdin:
            raise RuntimeError("MCP server is not running.")
        data = json.dumps(message).encode() + b"\n"
        self._process.stdin.write(data)
        await self._process.stdin.drain()

    async def _read_response(self, req_id: int) -> Dict[str, Any]:
        """Read lines from stdout, skip notifications, return the response matching req_id."""
        if not self._process or not self._process.stdout:
            raise RuntimeError("MCP server is not running.")
        deadline = asyncio.get_event_loop().time() + RESPONSE_TIMEOUT
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise MCPError("MCP server did not respond within timeout.")
            try:
                line = await asyncio.wait_for(self._process.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                raise MCPError("MCP server did not respond within timeout.")
            if not line:
                raise MCPError("MCP server closed stdout unexpectedly.")
            msg = json.loads(line.decode().strip())
            # Skip notifications (no "id" field) — server can send these at any time
            if "id" not in msg:
                logger.debug("MCP notification skipped: %s", msg.get("method", "unknown"))
                continue
            if msg["id"] == req_id:
                return msg
            logger.warning("MCP unexpected response id=%s (expected %d)", msg.get("id"), req_id)

    async def _request(self, method: str, params: Dict[str, Any]) -> Any:
        """Send a JSON-RPC request and return the result."""
        req_id = self._next_id()
        await self._write({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        })
        response = await self._read_response(req_id)
        if "error" in response:
            raise MCPError(f"MCP RPC error ({method}): {response['error']}")
        return response.get("result", {})

    async def _notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        await self._write({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        })


async def shutdown_all_mcp_clients() -> None:
    """Terminate all live MCP clients. Called from FastAPI lifespan shutdown."""
    for client in list(_live_clients):
        await client.shutdown()
