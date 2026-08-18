"""
Plugin Registry for Jarvis AI Assistant.

Discovers, loads, and manages plugins. Owns the canonical capability catalog.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import logging
import pkgutil
import re
import sys
from collections.abc import Iterable
from dataclasses import replace
from typing import Any, Callable, Dict, Iterator, Set, get_type_hints
from importlib.metadata import version, PackageNotFoundError

from pydantic import BaseModel, ConfigDict, TypeAdapter, create_model

from core.decorators import get_tool_meta
from core.plugins.capabilities import CapabilityDefinition, CapabilitySource
from core.plugins.types import JarvisPlugin
from core.routing.helpers import schema_chars_to_tokens

logger = logging.getLogger(__name__)

_DISABLED_PLUGINS_KEY = "disabled_plugins"
_PROVIDER_NAME_MAX = 64
_UNSAFE_PROVIDER_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_EMPTY_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _validate_dependencies(plugin_instance: JarvisPlugin) -> bool:
    """Check if plugin's declared dependencies are installed."""
    deps = plugin_instance.metadata.dependencies
    if not deps:
        return True

    missing = []
    for dep in deps:
        try:
            version(dep)
        except PackageNotFoundError:
            missing.append(dep)

    if missing:
        logger.warning(
            f"Plugin '{plugin_instance.name}' requires missing packages: {missing}. "
            f"Install with: uv add {' '.join(missing)}"
        )
        return False
    return True


def _visible_signature_for(func: Callable[..., Any]) -> inspect.Signature:
    meta = get_tool_meta(func)
    if meta:
        sig = meta["signature"]
        hidden: set[str] = {"self", *meta["inject"]}
        visible = [p for p in sig.parameters.values() if p.name not in hidden]
        return sig.replace(parameters=visible)

    sig = getattr(func, "__signature__", None) or inspect.signature(func)
    visible = [p for p in sig.parameters.values() if p.name != "self"]
    return sig.replace(parameters=visible)


def _capability_source_for(plugin: JarvisPlugin) -> CapabilitySource:
    source = getattr(plugin, "_capability_source", None)
    if source in {"first_party", "mcp", "eval"}:
        return source  # type: ignore[return-value]
    return "first_party"


def encode_provider_name(fqn: str) -> str:
    """Encode an internal FQN into a provider-safe function name."""
    encoded = _UNSAFE_PROVIDER_CHARS.sub("_", fqn.replace(".", "__"))
    if not encoded or not encoded[0].isalpha():
        encoded = f"t_{encoded}"
    if len(encoded) <= _PROVIDER_NAME_MAX:
        return encoded
    return _hashed_provider_name(fqn)


def _hashed_provider_name(fqn: str) -> str:
    digest = hashlib.sha1(fqn.encode()).hexdigest()[:8]
    encoded = _UNSAFE_PROVIDER_CHARS.sub("_", fqn.replace(".", "__"))
    if not encoded or not encoded[0].isalpha():
        encoded = f"t_{encoded}"
    prefix = encoded[: _PROVIDER_NAME_MAX - 9].rstrip("_")
    return f"{prefix}_{digest}"


def _unique_provider_names(definitions: list[CapabilityDefinition]) -> list[CapabilityDefinition]:
    used: dict[str, str] = {}
    unique: list[CapabilityDefinition] = []
    for definition in definitions:
        name = definition.provider_name or encode_provider_name(definition.fqn)
        if name in used:
            name = _hashed_provider_name(definition.fqn)
            if name in used:
                raise ValueError(
                    f"Provider name collision: {definition.fqn} vs {used[name]} ({name})"
                )
            definition = replace(definition, provider_name=name)
        used[name] = definition.fqn
        unique.append(definition)
    return unique


def _concise_description(documentation: str, fallback: str) -> str:
    first = (documentation or "").strip().split("\n", 1)[0].strip()
    return first or fallback


def _build_input_model(
    fqn: str,
    signature: inspect.Signature,
    func: Callable[..., Any],
) -> type[BaseModel]:
    target = getattr(func, "__func__", func)
    hints: dict[str, Any] = {}
    module = sys.modules.get(getattr(target, "__module__", ""), None)
    globalns = getattr(module, "__dict__", {})
    try:
        hints = get_type_hints(target, globalns=globalns, localns=globalns)
    except Exception:
        hints = {}

    fields: dict[str, Any] = {}
    for name, param in signature.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(name, param.annotation)
        if annotation is inspect.Parameter.empty:
            annotation = Any
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, default)

    model_name = re.sub(r"[^0-9A-Za-z_]", "_", fqn) + "_Input"
    try:
        return create_model(
            model_name,
            __config__=ConfigDict(extra="forbid"),
            **fields,
        )  # type: ignore[call-overload]
    except Exception:
        fallback = {
            name: (
                Any,
                ... if param.default is inspect.Parameter.empty else param.default,
            )
            for name, param in signature.parameters.items()
            if param.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        }
        return create_model(
            model_name,
            __config__=ConfigDict(extra="forbid"),
            **fallback,
        )  # type: ignore[call-overload]


def _schema_from_model(model: type[BaseModel]) -> dict[str, Any]:
    try:
        schema = TypeAdapter(model).json_schema()
    except Exception:
        return dict(_EMPTY_INPUT_SCHEMA)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return dict(_EMPTY_INPUT_SCHEMA)
    return schema


def build_capability_definition(
    plugin: JarvisPlugin,
    tool_name: str,
    func: Callable[..., Any],
    *,
    enabled: bool,
) -> CapabilityDefinition:
    meta = get_tool_meta(func)
    source = _capability_source_for(plugin)
    mcp_annotations = getattr(func, "_mcp_annotations", None)
    trusted_mcp = bool(getattr(func, "_mcp_trusted", False))
    injected = tuple(meta["inject"]) if meta else ()
    visible_signature = _visible_signature_for(func)
    documentation = inspect.cleandoc(func.__doc__ or "")
    fqn = f"{plugin.name}.{tool_name}"
    mcp_schema = getattr(func, "_mcp_input_schema", None)
    input_model = _build_input_model(fqn, visible_signature, func)
    if isinstance(mcp_schema, dict) and mcp_schema:
        input_schema = dict(mcp_schema)
    else:
        input_schema = _schema_from_model(input_model)
    return CapabilityDefinition(
        fqn=fqn,
        plugin=plugin.name,
        name=tool_name,
        implementation=func,
        visible_signature=visible_signature,
        documentation=documentation,
        return_schema=dict(meta["return_schema"]) if meta else {},
        source=source,
        enabled=enabled,
        mcp_annotations=dict(mcp_annotations) if isinstance(mcp_annotations, dict) else None,
        trusted_mcp=trusted_mcp,
        provider_name=encode_provider_name(fqn),
        description=_concise_description(documentation, tool_name),
        injected=injected,
        input_schema=input_schema,
        input_model=input_model,
    )


class PluginRegistry:
    """Discovers, loads, and manages the lifecycle of Jarvis plugins."""

    def __init__(self):
        self.plugins: Dict[str, JarvisPlugin] = {}
        self._disabled: Set[str] = set()
        self._bespoke: Set[str] = set()  # plugins loaded from plugins/ directory
        self._capabilities: Dict[str, CapabilityDefinition] = {}
        self._provider_names: Dict[str, str] = {}

    @property
    def bespoke_names(self) -> frozenset[str]:
        """Read-only set of plugin names loaded from the plugins/ directory."""
        return frozenset(self._bespoke)

    def is_bespoke(self, name: str) -> bool:
        """Return True if the plugin was loaded from the plugins/ directory."""
        return name in self._bespoke

    # ------------------------------------------------------------------
    # Capability catalog
    # ------------------------------------------------------------------

    def rebuild_capabilities(self) -> None:
        """Rebuild the canonical capability map from registered plugins."""
        capabilities: Dict[str, CapabilityDefinition] = {}
        for plugin_name, plugin in self.plugins.items():
            enabled = self.is_enabled(plugin_name)
            for tool_name, func in plugin.get_tools().items():
                definition = build_capability_definition(
                    plugin,
                    tool_name,
                    func,
                    enabled=enabled,
                )
                capabilities[definition.fqn] = definition
        self._capabilities = {
            definition.fqn: definition
            for definition in _unique_provider_names(list(capabilities.values()))
        }
        self._provider_names = {
            definition.provider_name: definition.fqn
            for definition in self._capabilities.values()
        }

    def get_capability(self, fqn: str) -> CapabilityDefinition | None:
        if not self._capabilities and self.plugins:
            self.rebuild_capabilities()
        return self._capabilities.get(fqn)

    def resolve_provider_name(self, name: str) -> CapabilityDefinition | None:
        if not self._capabilities and self.plugins:
            self.rebuild_capabilities()
        fqn = self._provider_names.get(name)
        if fqn is None:
            return None
        return self._capabilities.get(fqn)

    def provider_tools(self, fqns: Iterable[str]) -> list[dict[str, Any]]:
        """LiteLLM/OpenAI-compatible tool definitions for an explicit FQN set."""
        if not self._capabilities and self.plugins:
            self.rebuild_capabilities()
        tools: list[dict[str, Any]] = []
        for fqn in sorted(fqns):
            definition = self._capabilities.get(fqn)
            if definition is None or not definition.enabled:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": definition.provider_name,
                    "description": definition.description or definition.name,
                    "parameters": definition.input_schema or dict(_EMPTY_INPUT_SCHEMA),
                },
            })
        return tools

    def estimate_schema_stats(self, fqns: Iterable[str]) -> tuple[int, int]:
        """JSON-schema token budget for the given FQNs."""
        payload = self.provider_tools(fqns)
        if not payload:
            return 0, 0
        chars = len(json.dumps(payload, separators=(",", ":")))
        return chars, schema_chars_to_tokens(chars)

    def iter_capabilities(
        self,
        *,
        enabled_only: bool = True,
    ) -> Iterator[CapabilityDefinition]:
        if not self._capabilities and self.plugins:
            self.rebuild_capabilities()
        for definition in self._capabilities.values():
            if enabled_only and not definition.enabled:
                continue
            yield definition

    def capabilities_for_plugin(
        self,
        plugin_name: str,
        *,
        enabled_only: bool = True,
    ) -> list[CapabilityDefinition]:
        return [
            definition
            for definition in self.iter_capabilities(enabled_only=enabled_only)
            if definition.plugin == plugin_name
        ]

    # ------------------------------------------------------------------
    # Enable / disable
    # ------------------------------------------------------------------

    def is_enabled(self, name: str) -> bool:
        return name not in self._disabled

    async def load_disabled(self) -> None:
        """Load persisted disabled plugin names from MongoDB on startup."""
        try:
            from services.database.mongodb import mongodb
            col = mongodb.get_collection("system_config")
            doc = await col.find_one({"_id": _DISABLED_PLUGINS_KEY})
            self._disabled = set(doc["names"]) if doc else set()
            if self._disabled:
                logger.info("Loaded %d disabled plugin(s): %s", len(self._disabled), self._disabled)
        except Exception as e:
            logger.warning("Could not load disabled plugins from DB: %s", e)

    async def set_plugin_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a plugin and persist the change to MongoDB."""
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)

        try:
            from services.database.mongodb import mongodb
            col = mongodb.get_collection("system_config")
            await col.update_one(
                {"_id": _DISABLED_PLUGINS_KEY},
                {"$set": {"names": list(self._disabled)}},
                upsert=True,
            )
        except Exception as e:
            logger.warning("Could not persist disabled plugins to DB: %s", e)

        self.rebuild_capabilities()
        logger.info("Plugin '%s' %s", name, "enabled" if enabled else "disabled")

    async def register(self, plugin: "JarvisPlugin") -> bool:
        """Programmatic registration for runtime-created plugins (e.g. MCP auto-bridge).

        Returns True if the plugin was registered, False if a plugin with the same
        name already exists in the registry (bespoke plugin from plugins/ always wins).
        """
        if plugin.name in self.plugins:
            logger.info(
                "Skipping auto-bridge '%s' — bespoke plugin already registered", plugin.name
            )
            return False
        if not _validate_dependencies(plugin):
            logger.error("Skipping plugin '%s' due to missing dependencies", plugin.name)
            return False
        await plugin.register_integrations()
        await plugin.initialize()
        self.plugins[plugin.name] = plugin
        self.rebuild_capabilities()
        logger.info("Registered plugin: %s", plugin.name)
        return True

    async def load_plugins(self) -> None:
        """Auto-discover and load plugins from the plugins directory."""
        from core import settings

        abs_plugins_dir = settings.PLUGINS_DIR

        if not abs_plugins_dir.exists():
            logger.warning(f"Plugins directory not found: {abs_plugins_dir}")
            return

        logger.info(f"Scanning for plugins in: {abs_plugins_dir}")

        for _, name, _ in pkgutil.iter_modules([str(abs_plugins_dir)]):
            try:
                module_name = f"plugins.{name}"
                module = importlib.import_module(module_name)

                for _, obj in inspect.getmembers(module):
                    if (inspect.isclass(obj) and
                        issubclass(obj, JarvisPlugin) and
                        obj is not JarvisPlugin):

                        plugin_instance = obj()

                        if not _validate_dependencies(plugin_instance):
                            logger.error(f"Skipping plugin {plugin_instance.name} due to missing dependencies")
                            continue

                        await plugin_instance.register_integrations()
                        await plugin_instance.initialize()

                        self.plugins[plugin_instance.name] = plugin_instance
                        self._bespoke.add(plugin_instance.name)
                        logger.info(f"Loaded plugin: {plugin_instance.name}")

            except Exception as e:
                logger.error(f"Failed to load plugin {name}: {e}", exc_info=True)

        self.rebuild_capabilities()

    async def deregister(self, name: str) -> bool:
        """Remove a plugin from the registry and run teardown.

        Bespoke plugins (loaded from plugins/) are never deregisterable — they
        are only managed via enable/disable. Returns False if the plugin is
        bespoke or not found.
        """
        if name in self._bespoke:
            logger.warning("Cannot deregister bespoke plugin '%s'", name)
            return False

        plugin = self.plugins.pop(name, None)
        if plugin is None:
            return False

        try:
            await plugin.shutdown()
        except Exception as e:
            logger.warning("Plugin '%s' shutdown failed during deregister: %s", name, e)

        self.rebuild_capabilities()

        logger.info("Deregistered plugin: %s", name)
        return True


# Global registry instance
registry = PluginRegistry()
