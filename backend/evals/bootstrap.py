"""Shared bootstrap for offline eval tool manifests.

Eval runners need plugin metadata and tool signatures for routing/manifests, but
they should not run plugin startup side effects like database recovery jobs.
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import pkgutil
from typing import Any

from core.config import settings
from core.integrations.mcp.bridge import generate_utterances
from core.integrations.mcp.cache import load_cached_schema
from core.integrations.mcp.config import load_mcp_config
from core.plugins.registry import registry
from core.plugins.types import JarvisPlugin, PluginMetadata

logger = logging.getLogger(__name__)
_READY = False


class _EvalMCPPlugin(JarvisPlugin, register=False):
    metadata: PluginMetadata  # type: ignore[assignment]
    _capability_source = "eval"

    def __init__(
        self,
        name: str,
        tools: list[dict[str, Any]],
        utterances: list[str],
    ) -> None:
        self.metadata = PluginMetadata(
            name=name,
            description=f"Cached MCP/Composio server: {name}",
            utterances=utterances,
        )
        self._tools = {tool["name"]: self._make_tool(tool) for tool in tools}

    @staticmethod
    def _make_tool(tool_schema: dict[str, Any]):
        from core.integrations.mcp.bridge import _schema_to_signature

        input_schema = tool_schema.get("inputSchema") or tool_schema.get("input_schema") or {}
        sig = _schema_to_signature(input_schema)

        async def _tool(**_kwargs: Any) -> Any:
            raise RuntimeError("Offline eval tool stubs are not executable")

        _tool.__name__ = tool_schema["name"]
        _tool.__doc__ = tool_schema.get("description") or f"Call {tool_schema['name']}."
        _tool.__signature__ = sig  # type: ignore[attr-defined]
        _tool._tool_meta = {  # type: ignore[attr-defined]
            "inject": (),
            "signature": sig,
            "return_schema": {},
        }
        _tool._mcp_input_schema = dict(input_schema) if isinstance(input_schema, dict) else {}  # type: ignore[attr-defined]
        return _tool

    def get_tools(self) -> dict[str, Any]:
        return dict(self._tools)


async def ensure_eval_plugins_loaded() -> None:
    """Register plugin tool manifests for evals without plugin startup hooks."""
    global _READY
    if _READY:
        return

    _load_local_plugins_for_eval()
    _load_cached_mcp_plugins_for_eval()
    registry.rebuild_capabilities()
    _READY = True


def _load_local_plugins_for_eval() -> None:
    plugins_dir = settings.PLUGINS_DIR
    if not plugins_dir.exists():
        logger.warning("Plugins directory not found: %s", plugins_dir)
        return

    for _, name, _ in pkgutil.iter_modules([str(plugins_dir)]):
        if name in registry.plugins:
            continue
        try:
            module = importlib.import_module(f"plugins.{name}")
            for _, obj in inspect.getmembers(module):
                if not (
                    inspect.isclass(obj)
                    and issubclass(obj, JarvisPlugin)
                    and obj is not JarvisPlugin
                ):
                    continue
                plugin = obj()
                registry.plugins[plugin.name] = plugin
                registry._bespoke.add(plugin.name)
                logger.info("Loaded eval plugin manifest: %s", plugin.name)
        except Exception as exc:
            logger.warning("Skipping eval plugin manifest %s: %s", name, exc)


def _load_cached_schema_for_eval(name: str) -> list[dict[str, Any]]:
    """Load cached MCP schemas even if TTL-expired.

    Runtime startup should respect the 24h TTL. Offline evals need a stable local
    approximation of connected tool namespaces and should not fail oracle cases
    just because a cached schema is old.
    """
    fresh = load_cached_schema(name)
    if fresh:
        return fresh

    path = settings.CACHE_DIR / "mcp_schemas" / f"{name}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
        tools = payload.get("tools")
        return tools if isinstance(tools, list) else []
    except Exception:
        return []


def _register_eval_mcp_plugin(
    name: str,
    tools: list[dict[str, Any]],
    utterances: list[str],
) -> bool:
    if name in registry.plugins:
        return False
    registry.plugins[name] = _EvalMCPPlugin(name, tools, utterances)
    logger.info("Loaded eval MCP manifest: %s", name)
    return True


def _load_cached_mcp_plugins_for_eval() -> None:
    """Register cached MCP/Composio schemas so offline routing sees connected namespaces."""
    loaded: set[str] = set()
    config_by_name = {
        config.name: config
        for config in load_mcp_config(settings.MCP_SERVERS_CONFIG)
        if config.type == "composio"
    }

    for name, config in config_by_name.items():
        raw_tools = _load_cached_schema_for_eval(name)
        if not raw_tools:
            continue
        tools = [tool for tool in raw_tools if not config.tools or tool["name"] in config.tools]
        utterances = config.utterances or generate_utterances(raw_tools, name)
        if _register_eval_mcp_plugin(name, tools, utterances):
            loaded.add(name)


    cache_dir = settings.CACHE_DIR / "mcp_schemas"
    if not cache_dir.exists():
        return
    for path in sorted(cache_dir.glob("*.json")):
        name = path.stem
        if name in loaded or name in registry.plugins:
            continue
        raw_tools = _load_cached_schema_for_eval(name)
        if not raw_tools:
            continue
        _register_eval_mcp_plugin(name, raw_tools, generate_utterances(raw_tools, name))
