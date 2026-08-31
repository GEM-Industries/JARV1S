"""
MCP Auto-Bridge: converts MCP server tools/list into JarvisPlugin instances.

MCPBridgePlugin exposes tools as `jarvis.<server>.*` capabilities.
Tool routing and the per-turn tools= set are handled by ToolRouter using
hybrid utterance + description embeddings. This class is a thin wrapper: get_tools()
returns all mounted tools, period.
"""

import asyncio
import inspect
import logging
import re
from typing import Any, Callable, Literal, Optional

from core.integrations.mcp.cache import (
    invalidate_cache,
    load_cached_schema,
    save_schema_cache,
)
from core.integrations.mcp.client import MCPClient, MCPError
from core.integrations.mcp.config import (
    MCPConfigError,
    MCPServerConfig,
    load_mcp_config,
    load_runtime_mcp_servers,
)
from core.integrations.mcp.http_client import StreamableHTTPClient
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.types import JarvisPlugin, PluginMetadata

_MCPTransport = MCPClient | StreamableHTTPClient

logger = logging.getLogger(__name__)


def _unwrap_composio_response(result: Any) -> Any:
    """Strip Composio's {successfull, data: {data: ...}} envelope if present.

    Non-Composio responses pass through unchanged (guarded by 'successfull' key check).
    When the outer 'data' dict contains only a nested 'data' key, unwrap one level
    so the LLM receives flat, directly-accessible payloads.
    """
    if not isinstance(result, dict) or "successfull" not in result:
        return result
    if not result.get("successfull"):
        error = result.get("error", result.get("data", "Unknown error"))
        message = error if isinstance(error, str) else str(error)
        return CapabilityErrorDetail(code="tool_error", message=message)
    data = result.get("data", result)
    if isinstance(data, dict) and list(data.keys()) == ["data"]:
        return data["data"]
    return data


_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

# ---------------------------------------------------------------------------
# Signature generation
# ---------------------------------------------------------------------------


def _annotate(prop_schema: dict[str, Any]) -> Any:
    """Map a JSON Schema property to a Python annotation, preserving string enums.

    Surfaces enum values as Literal[...] so the LLM-visible signature shows
    valid choices (e.g. travelMode: Literal['DRIVE','WALK','TRANSIT']).
    """
    base = _TYPE_MAP.get(prop_schema.get("type", "string"), str)
    enum_vals = prop_schema.get("enum")
    if enum_vals and base is str:
        try:
            return Literal[tuple(enum_vals)]  # type: ignore[valid-type]
        except TypeError:
            return base
    return base


def _schema_to_signature(input_schema: dict[str, Any]) -> inspect.Signature:
    """Convert a JSON Schema object into an inspect.Signature.

    Required properties become KEYWORD_ONLY params with no default.
    Optional properties become KEYWORD_ONLY params with default=None.
    String enums are preserved as Literal[...] so the LLM sees valid values.
    """
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))

    required_params: list[inspect.Parameter] = []
    optional_params: list[inspect.Parameter] = []

    for name, prop_schema in properties.items():
        py_type = _annotate(prop_schema)
        if name in required:
            required_params.append(
                inspect.Parameter(
                    name=name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    annotation=py_type,
                )
            )
        else:
            optional_params.append(
                inspect.Parameter(
                    name=name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    default=None,
                    annotation=Optional[py_type],
                )
            )

    return inspect.Signature(required_params + optional_params)


def _build_args_block(input_schema: dict[str, Any]) -> str:
    """Render an `Args:` block from per-property descriptions/enums.

    Native plugin tools surface param semantics via docstring `Args:` sections;
    MCP tools have no equivalent unless we synthesize one from JSON Schema.
    Returns "" when no property has a description or enum.
    """
    properties = input_schema.get("properties") or {}
    lines: list[str] = []
    for pname, pschema in properties.items():
        bits: list[str] = []
        if d := pschema.get("description"):
            first = d.split("\n", 1)[0].strip()
            if first:
                bits.append(first[:140])
        if e := pschema.get("enum"):
            bits.append(f"one of {list(e)}")
        if bits:
            lines.append(f"  {pname}: {' — '.join(bits)}")
    return "Args:\n" + "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Tool function generation
# ---------------------------------------------------------------------------


def _generate_tool_fn(
    client: _MCPTransport,
    tool_name: str,
    tool_schema: dict[str, Any],
    server_name: str,
    *,
    trusted: bool = False,
) -> Callable:
    """Return an async callable for a single MCP tool with __signature__ and __doc__ set."""
    input_schema = tool_schema.get("inputSchema") or tool_schema.get("input_schema") or {}
    sig = _schema_to_signature(input_schema)
    annotations = tool_schema.get("annotations")
    if not isinstance(annotations, dict):
        annotations = None

    async def tool_fn(**kwargs: Any) -> Any:
        try:
            raw = await client.call_tool(tool_name, kwargs)
            return _unwrap_composio_response(raw)
        except MCPError as e:
            err_str = str(e).lower()
            # Only treat as tool-not-found if it's an RPC-level error, not an
            # application-level isError response (which has the "mcp tool '...' error:" prefix).
            is_app_error = err_str.startswith("mcp tool '")
            if not is_app_error and ("not found" in err_str or "unknown tool" in err_str):
                invalidate_cache(server_name)
                raise RuntimeError(
                    f"Tool '{tool_name}' not found on MCP server '{server_name}'. "
                    "Schema cache has been invalidated — restart to refresh tool list."
                ) from e
            raise

    description = tool_schema.get("description", f"Call {tool_name} on {server_name}.")
    args_block = _build_args_block(input_schema)
    if args_block:
        description = f"{description}\n\n{args_block}"

    tool_fn.__signature__ = sig
    tool_fn.__doc__ = description
    tool_fn.__name__ = tool_name
    tool_fn.__qualname__ = f"{server_name}.{tool_name}"
    tool_fn._tool_meta = {  # type: ignore[attr-defined]
        "inject": (),
        "signature": sig,
        "return_schema": {},
    }
    # MCP annotations are untrusted hints unless the server is explicitly trusted.
    tool_fn._mcp_annotations = annotations  # type: ignore[attr-defined]
    tool_fn._mcp_trusted = trusted  # type: ignore[attr-defined]
    tool_fn._mcp_input_schema = dict(input_schema) if isinstance(input_schema, dict) else {}  # type: ignore[attr-defined]

    return tool_fn


# ---------------------------------------------------------------------------
# Utterance generation
# ---------------------------------------------------------------------------


def generate_utterances(tools: list[dict[str, Any]], server_name: str) -> list[str]:
    """Generate synthetic utterances from tool names and descriptions for the ToolRouter.

    Hand-curate utterances in mcp_servers.json for important servers.
    """
    utterances = [server_name]
    phrase_utterances: list[str] = []
    keywords: set[str] = set()

    _stop = {"with", "from", "that", "this", "list", "get", "set", "the", "and", "for"}

    for tool in tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")

        words = re.sub(r"[_\-]", " ", name)
        words = re.sub(r"([a-z])([A-Z])", r"\1 \2", words).lower()
        keywords.update(w for w in words.split() if len(w) > 3 and w not in _stop)

        if desc:
            first = desc.split(".")[0].strip()
            if first and len(first) < 80:
                phrase_utterances.append(first)

    utterances.extend(phrase_utterances[:8])
    utterances.extend(list(keywords)[:8])

    return utterances[:20]


# ---------------------------------------------------------------------------
# MCPBridgePlugin
# ---------------------------------------------------------------------------


class MCPBridgePlugin(JarvisPlugin, register=False):
    """A JarvisPlugin populated at runtime from an MCP server's tools/list schema.

    Thin wrapper — get_tools() returns all mounted tools. Tool routing and
    the per-turn tools= set are handled by ToolRouter via hybrid embeddings.

    Declared with ``register=False`` because the plugin name and tool set
    come from the MCP server schema at ``__init__`` time, not a class-level
    ``PluginMetadata`` — and MCP server names may not match Python module
    naming rules.
    """

    # Instance-assigned in __init__; shadows the class-level contract.
    metadata: PluginMetadata  # type: ignore[assignment]
    _capability_source = "mcp"

    def __init__(
        self,
        server_name: str,
        client: _MCPTransport,
        all_tools: list[dict[str, Any]],
        utterances: list[str],
        *,
        trusted: bool = False,
    ) -> None:
        self._server_name = server_name
        self._client = client
        self._trusted = trusted
        self.metadata = PluginMetadata(
            name=server_name,
            description=f"Auto-bridged MCP server: {server_name}",
            utterances=utterances,
        )

        self._tools_map: dict[str, Callable] = {
            t["name"]: _generate_tool_fn(
                client,
                t["name"],
                t,
                server_name,
                trusted=trusted,
            )
            for t in all_tools
        }

        logger.info(
            "MCPBridgePlugin '%s': %d tools mounted",
            server_name, len(self._tools_map),
        )

    def get_tools(self) -> dict[str, Callable]:
        return dict(self._tools_map)

    async def shutdown(self) -> None:
        await self._client.shutdown()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_client(config: MCPServerConfig) -> _MCPTransport:
    if config.url:
        return StreamableHTTPClient(url=config.url, headers=config.env or {})
    return MCPClient(server_command=config.command, env=config.env or {})


async def _connect_and_fetch(config: MCPServerConfig) -> Optional[MCPBridgePlugin]:
    """Connect to an MCP server and return an MCPBridgePlugin, or None on failure."""
    server_name = config.name
    client = _make_client(config)

    cached = load_cached_schema(server_name)
    if cached is not None:
        logger.info("MCP '%s': loaded %d tools from cache", server_name, len(cached))
        await client.start()
        asyncio.create_task(_refresh_cache(client, server_name))
        raw_tools = cached
    else:
        await client.start()
        raw_tools = await client.list_tools()
        save_schema_cache(server_name, raw_tools)
        logger.info("MCP '%s': fetched %d tools from server", server_name, len(raw_tools))

    if config.tools:
        tools = [t for t in raw_tools if t["name"] in config.tools]
        logger.debug(
            "MCP '%s': allowlist filtered to %d / %d tools",
            server_name, len(tools), len(raw_tools),
        )
    else:
        tools = raw_tools

    utterances = config.utterances or generate_utterances(raw_tools, server_name)

    return MCPBridgePlugin(
        server_name=server_name,
        client=client,
        all_tools=tools,
        utterances=utterances,
        trusted=bool(config.trusted),
    )


async def _refresh_cache(client: _MCPTransport, server_name: str) -> None:
    try:
        fresh = await client.list_tools()
        save_schema_cache(server_name, fresh)
        logger.debug("MCP '%s': background cache refreshed (%d tools)", server_name, len(fresh))
    except Exception as e:
        logger.warning("MCP '%s': background cache refresh failed: %s", server_name, e)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

_live_bridge_names: frozenset[str] = frozenset()


def live_bridge_names() -> frozenset[str]:
    return _live_bridge_names


async def load_mcp_bridges(configs: list[MCPServerConfig] | None = None) -> list[str]:
    """Load stdio/HTTP MCP servers and register as auto-bridged plugins.

    Composio-type entries are skipped — managed by ComposioGateway.
    Bespoke plugins with matching names always win on collision.
    """
    global _live_bridge_names
    from core.config import settings
    from core.plugins.registry import registry

    if configs is None:
        try:
            configs = load_runtime_mcp_servers()
        except MCPConfigError as exc:
            logger.error("MCP merge rejected; using packaged config only: %s", exc)
            try:
                configs = load_mcp_config(settings.MCP_SERVERS_CONFIG)
            except MCPConfigError as packaged_exc:
                logger.error("Packaged MCP config invalid: %s", packaged_exc)
                configs = []

    registered: list[str] = []
    for config in [c for c in configs if c.type != "composio"]:
        try:
            plugin = await _connect_and_fetch(config)
            if plugin:
                if await registry.register(plugin):
                    registered.append(config.name)
                    logger.info(
                        "Auto-bridge ready: jarvis.%s (%d tools)",
                        config.name, len(plugin.get_tools()),
                    )
        except Exception as e:
            logger.error(
                "Failed to load MCP server '%s': %s — skipping", config.name, e, exc_info=True,
            )

    _live_bridge_names = frozenset(registered)
    return registered
