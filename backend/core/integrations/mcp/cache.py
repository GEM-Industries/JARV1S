"""
Schema cache for MCP tools/list responses.

Caches tool schemas to disk with a 24-hour TTL so repeated restarts
don't reconnect to every MCP server from scratch.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CACHE_TTL = 86400  # 24 hours


def _cache_dir() -> Path:
    from core.config import settings
    return settings.CACHE_DIR / "mcp_schemas"


def _cache_path(server_name: str) -> Path:
    return _cache_dir() / f"{server_name}.json"


def load_cached_schema(server_name: str) -> Optional[list[dict[str, Any]]]:
    """Return cached tools list if present and < 24h old. Returns None otherwise."""
    path = _cache_path(server_name)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
        age = time.time() - data.get("timestamp", 0)
        if age > _CACHE_TTL:
            logger.debug(
                "MCP schema cache expired for '%s' (%.0f h old)", server_name, age / 3600
            )
            return None
        return data["tools"]
    except Exception as e:
        logger.warning("Failed to read MCP schema cache for '%s': %s", server_name, e)
        return None


def save_schema_cache(server_name: str, tools: list[dict[str, Any]]) -> None:
    """Persist a tools/list response to the on-disk cache."""
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(server_name)
    try:
        path.write_text(json.dumps({"timestamp": time.time(), "tools": tools}, indent=2))
    except Exception as e:
        logger.warning("Failed to write MCP schema cache for '%s': %s", server_name, e)


def invalidate_cache(server_name: str) -> None:
    """Delete the cached schema for a server (triggers fresh fetch on next startup)."""
    path = _cache_path(server_name)
    if path.exists():
        path.unlink()
        logger.info("Invalidated MCP schema cache for '%s'", server_name)
