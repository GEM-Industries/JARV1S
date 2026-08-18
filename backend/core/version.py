"""Jarvis Host version and runtime dependency metadata."""

from __future__ import annotations

import platform
import tomllib
from pathlib import Path
from typing import TypedDict

from core.config import settings

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent
_BACKEND_PYPROJECT = _BACKEND_DIR / "pyproject.toml"
_NODE_VERSION_FILE = _REPO_ROOT / ".nvmrc"

MONGODB_VERSION = "8.2"


class VersionInfo(TypedDict):
    app: str
    api: str
    python: str
    node: str | None
    mongodb: str


def _read_backend_version() -> str:
    with _BACKEND_PYPROJECT.open("rb") as file:
        data = tomllib.load(file)
    return str(data["project"]["version"])


def _read_node_version() -> str | None:
    if not _NODE_VERSION_FILE.exists():
        return None
    return _NODE_VERSION_FILE.read_text(encoding="utf-8").strip() or None


def get_version_info() -> VersionInfo:
    """Return the support/debug version surface for the local Jarvis Host."""
    return {
        "app": _read_backend_version(),
        "api": settings.API_V1_STR.strip("/"),
        "python": platform.python_version(),
        "node": _read_node_version(),
        "mongodb": MONGODB_VERSION,
    }
