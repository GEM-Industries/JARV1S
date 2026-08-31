"""MCP server configuration loader.

Reads the packaged mcp_servers.json and optional home/mcp.json extras.
Both files use the same `{ "servers": [...] }` JSON shape.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import AbstractSet, Any, Optional

from pydantic import BaseModel, model_validator

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_EXACT_ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_SECRET_KEY_RE = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|authorization|credential)",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9]|ghp_|github_pat_|xox[baprs]-|-----BEGIN)",
    re.IGNORECASE,
)


class MCPConfigError(ValueError):
    """Raised when an MCP JSON document is invalid."""


def interpolate_env_value(value: str, environ: Mapping[str, str] | None = None) -> str:
    """Replace ${VAR} patterns. Missing variables become empty strings."""
    source = os.environ if environ is None else environ
    return _ENV_VAR_RE.sub(lambda match: source.get(match.group(1), ""), value)


def looks_like_inline_secret(key: str, value: str) -> bool:
    if not value or _EXACT_ENV_REF_RE.fullmatch(value.strip()):
        return False
    if _SECRET_VALUE_RE.search(value):
        return True
    return bool(_SECRET_KEY_RE.search(key))


class MCPServerConfig(BaseModel):
    name: str
    # stdio transport: list of command tokens, e.g. ["npx", "@mcp/server-github"]
    command: Optional[list[str]] = None
    # HTTP transport: full URL to the MCP server
    url: Optional[str] = None
    # Env vars passed to the subprocess. Keep ${VAR} references; interpolate at spawn.
    env: dict[str, str] = {}
    # Optional explicit allowlist of tool names; if omitted, all server tools are mounted
    tools: Optional[list[str]] = None
    # Hand-curated utterances for the Tool Router; auto-generated if omitted
    utterances: Optional[list[str]] = None
    # "composio" for managed-auth entries (skipped by stdio loader)
    type: Optional[str] = None
    # Composio trigger slugs to register automatically when this app connects.
    triggers: Optional[list[str]] = None
    # When true, MCP tool annotations may be surfaced as trusted hints.
    # Annotations are never used as authorization.
    trusted: bool = False

    @model_validator(mode="after")
    def _validate(self) -> "MCPServerConfig":
        if self.type not in ("composio",) and not self.command and not self.url:
            raise ValueError(
                f"Server '{self.name}' must specify 'command' (stdio) or 'url' (HTTP)"
            )
        if self.command and self.url:
            raise ValueError(
                f"Server '{self.name}' cannot specify both 'command' and 'url' — choose one transport"
            )
        for key, value in self.env.items():
            if looks_like_inline_secret(key, value):
                raise ValueError(
                    f"Server '{self.name}' env '{key}' looks like an inline secret; "
                    "use a ${VAR} reference"
                )
        return self


class _MCPConfig(BaseModel):
    servers: list[MCPServerConfig] = []


def parse_mcp_document(
    raw: Any,
    *,
    extras: bool = False,
    reserved_names: AbstractSet[str] | None = None,
) -> list[MCPServerConfig]:
    """Validate one `{servers: [...]}` document. Raises MCPConfigError on failure."""
    try:
        config = _MCPConfig.model_validate(raw)
    except Exception as exc:
        raise MCPConfigError(str(exc)) from exc

    reserved = set(reserved_names or ())
    seen: set[str] = set()
    for server in config.servers:
        if extras and server.type == "composio":
            raise MCPConfigError(
                f"home/mcp.json cannot declare Composio server '{server.name}'"
            )
        if extras and server.trusted:
            raise MCPConfigError(
                f"home/mcp.json cannot set trusted on server '{server.name}'"
            )
        if server.name in seen:
            raise MCPConfigError(f"Duplicate MCP server name '{server.name}'")
        if server.name in reserved:
            raise MCPConfigError(
                f"MCP server name '{server.name}' collides with a packaged or plugin name"
            )
        seen.add(server.name)
    return config.servers


def load_mcp_config(
    path: Path,
    *,
    extras: bool = False,
    reserved_names: AbstractSet[str] | None = None,
) -> list[MCPServerConfig]:
    """Load and validate MCP JSON. Missing file returns []. Invalid raises MCPConfigError."""
    if not path.exists():
        logger.debug("No MCP config at %s", path)
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MCPConfigError(f"Invalid MCP JSON at {path}: {exc}") from exc
    except OSError as exc:
        raise MCPConfigError(f"Unreadable MCP config at {path}: {exc}") from exc

    servers = parse_mcp_document(raw, extras=extras, reserved_names=reserved_names)
    logger.info("Loaded %d MCP server configs from %s", len(servers), path)
    return servers


def _bespoke_plugin_names() -> set[str]:
    from core.plugins.registry import registry

    return {
        name
        for name, plugin in registry.plugins.items()
        if getattr(plugin, "_capability_source", None) != "mcp"
    }


def load_runtime_mcp_servers() -> list[MCPServerConfig]:
    """Merge packaged mcp_servers.json with home/mcp.json extras.

    Raises MCPConfigError if either file is present and invalid so callers can
    keep the live set (refresh) or fall back to packaged-only (startup).
    """
    from core.config import settings
    from core.home import home_root

    packaged = load_mcp_config(settings.MCP_SERVERS_CONFIG)
    reserved = {server.name for server in packaged} | _bespoke_plugin_names()
    extras = load_mcp_config(
        home_root() / "mcp.json", extras=True, reserved_names=reserved
    )
    return packaged + extras
