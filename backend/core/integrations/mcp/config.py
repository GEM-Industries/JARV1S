"""
MCP server configuration loader.

Reads mcp_servers.yaml and validates each server entry.
Supports stdio (command) and HTTP (url) transports, per-server tool allowlists,
hand-curated utterances, and ${ENV_VAR} interpolation in env values.
"""

import os
import re
import logging
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, model_validator

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env(value: str) -> str:
    """Replace ${VAR} patterns with environment variable values."""
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


class MCPServerConfig(BaseModel):
    name: str
    # stdio transport: list of command tokens, e.g. ["npx", "@mcp/server-github"]
    command: Optional[list[str]] = None
    # HTTP transport (Phase 1b): full URL to the MCP server
    url: Optional[str] = None
    # Env vars passed to the subprocess (${VAR} interpolated from os.environ)
    env: dict[str, str] = {}
    # Optional explicit allowlist of tool names; if omitted, all server tools are mounted
    tools: Optional[list[str]] = None
    # Hand-curated utterances for the Tool Router; auto-generated if omitted
    utterances: Optional[list[str]] = None
    # "composio" for Phase 3 managed auth entries (skipped by stdio loader)
    type: Optional[str] = None
    # Composio trigger slugs to register automatically when this app connects.
    # e.g. ["SLACK_NEW_MESSAGE", "SLACK_NEW_MENTION"]. Only meaningful for
    # type: composio entries — ignored for stdio/HTTP servers.
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
        self.env = {k: _interpolate_env(v) for k, v in self.env.items()}
        return self


class _MCPConfig(BaseModel):
    servers: list[MCPServerConfig] = []


def load_mcp_config(path: Path) -> list[MCPServerConfig]:
    """Load and validate mcp_servers.yaml. Returns empty list if file not found."""
    if not path.exists():
        logger.debug("No mcp_servers.yaml at %s", path)
        return []

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    try:
        config = _MCPConfig.model_validate(raw)
        logger.info("Loaded %d MCP server configs from %s", len(config.servers), path)
        return config.servers
    except Exception as e:
        logger.error("Invalid mcp_servers.yaml: %s", e)
        return []
