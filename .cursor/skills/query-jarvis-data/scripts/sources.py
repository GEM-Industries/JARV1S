"""Resolve which JARV1S MongoDB to query (desktop app vs dev)."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "JARV1S"
APP_SOCKET = APP_SUPPORT / "run" / "mongodb-0.sock"
APP_LOGS = Path.home() / "Library" / "Logs" / "JARV1S"
DEV_URL = "mongodb://localhost:27018"

# .cursor/skills/query-jarvis-data/scripts → repo root
REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class DataSource:
    name: str  # app | dev
    mongodb_url: str
    data_dir: Path | None = None
    logs_dir: Path | None = None
    socket: Path | None = None


def app_source() -> DataSource:
    return DataSource(
        name="app",
        mongodb_url=f"mongodb://{quote(str(APP_SOCKET), safe='')}",
        data_dir=APP_SUPPORT,
        logs_dir=APP_LOGS,
        socket=APP_SOCKET,
    )


def dev_source() -> DataSource:
    return DataSource(
        name="dev",
        mongodb_url=os.environ.get("MONGODB_URL")
        or os.environ.get("JARVIS_MONGO_URL")
        or DEV_URL,
        data_dir=REPO_ROOT / ".data",
        logs_dir=REPO_ROOT / "backend" / "logs",
    )


def socket_up(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            sock.connect(str(path))
        return True
    except OSError:
        return False


async def ping(url: str) -> bool:
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=1500)
    try:
        await client.admin.command("ping")
        return True
    except Exception:
        return False
    finally:
        client.close()


async def pick_source(requested: str) -> DataSource:
    """Resolve one explicit source and fail when it is unavailable."""
    requested = (requested or "app").lower()
    if requested == "app":
        source = app_source()
        if source.socket and socket_up(source.socket) and await ping(source.mongodb_url):
            return source
        raise SystemExit(f"JARV1S app database is unavailable: {APP_SOCKET}")
    if requested == "dev":
        source = dev_source()
        if await ping(source.mongodb_url):
            return source
        raise SystemExit(f"JARV1S dev database is unavailable: {source.mongodb_url}")
    raise SystemExit(f"Unknown --source {requested!r}; use app or dev")


def format_source(source: DataSource) -> str:
    lines = [f"source: {source.name}", f"mongodb: {source.mongodb_url}"]
    if source.data_dir:
        lines.append(f"data_dir: {source.data_dir}")
    if source.logs_dir:
        lines.append(f"logs_dir: {source.logs_dir}")
    if source.socket:
        state = "up" if socket_up(source.socket) else "down — is JARV1S running?"
        lines.append(f"socket: {source.socket} ({state})")
    return "\n".join(lines)
