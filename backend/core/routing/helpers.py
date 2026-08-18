"""Small shared helpers for routing and eval code."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

TOKEN_CHAR_RATIO = 4


def schema_chars_to_tokens(chars: int) -> int:
    """Approximate JSON-schema tokens from serialized chars."""
    return max(1, chars // TOKEN_CHAR_RATIO) if chars else 0


def expand_plugins_to_fqns(plugin_names: Iterable[str], registry_obj: Any) -> set[str]:
    """Expand plugin names to enabled `plugin.tool` FQNs."""
    routed: set[str] = set()
    for plugin_name in plugin_names:
        plugin = registry_obj.plugins.get(plugin_name)
        if not plugin or not registry_obj.is_enabled(plugin_name):
            continue
        for tool_name in plugin.get_tools():
            routed.add(f"{plugin_name}.{tool_name}")
    return routed
