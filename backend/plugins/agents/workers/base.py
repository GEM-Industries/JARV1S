"""Minimal code-worker seam.

Adapters own vendor SDK connect/stream/cancel/dispose. JARV1S owns named work,
Mongo settlement, receipts, and triggers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional, Protocol

from core.credentials.store import credential_store

WorkerKind = Literal["claude_code", "cursor_local"]
WorkerEventKind = Literal["text", "tool_start", "tool_result", "external_handle"]


class WorkerStartupError(Exception):
    """The vendor run never started (auth, missing SDK, bridge, config)."""

    def __init__(self, message: str, *, code: str = "worker_start_failed") -> None:
        super().__init__(message)
        self.code = code


class WorkerRunError(Exception):
    """The vendor run started and then failed."""


@dataclass(frozen=True)
class CodeWorkSpec:
    prompt: str
    cwd: str
    max_turns: int
    mcp_servers: list[dict]
    system_prompt: str
    resume_session_id: Optional[str] = None
    max_budget_usd: Optional[float] = None
    title: str = ""


@dataclass
class WorkerEvent:
    kind: WorkerEventKind
    text: str | None = None
    tool: str | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    is_error: bool = False
    result_content: Any = None
    session_id: str | None = None
    external_run_id: str | None = None


@dataclass
class WorkerResult:
    text: str
    summary: str
    session_id: Optional[str] = None
    external_run_id: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    num_turns: Optional[int] = None
    usage: Optional[dict[str, Any]] = None


EventEmitter = Callable[[WorkerEvent], Awaitable[None]]


class CodeWorker(Protocol):
    kind: WorkerKind
    inspect_label: str
    inspect_binaries: tuple[str, ...]
    inspect_via: Literal["terminal", "ide"]

    async def execute(self, spec: CodeWorkSpec, emit: EventEmitter) -> WorkerResult: ...


def mcp_servers_by_name(servers: list[dict]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for server in servers:
        name = server.get("name")
        url = server.get("url")
        if not name or not url:
            continue
        entry: dict[str, Any] = {"type": server.get("type", "http"), "url": url}
        if server.get("headers"):
            entry["headers"] = server["headers"]
        mapped[str(name)] = entry
    return mapped


def has_cursor_key() -> bool:
    return bool(credential_store.get_stored_secret("CURSOR_API_KEY"))


def has_anthropic_key() -> bool:
    return bool(credential_store.get_stored_secret("ANTHROPIC_API_KEY"))


def default_worker_kind() -> WorkerKind | None:
    if has_cursor_key():
        return "cursor_local"
    if has_anthropic_key():
        return "claude_code"
    return None


def lineage_worker_kind(doc: dict[str, Any] | None) -> WorkerKind:
    kind = str((doc or {}).get("worker_kind") or "").strip()
    if kind == "cursor_local":
        return "cursor_local"
    return "claude_code"


def worker_ready(kind: WorkerKind) -> bool:
    if kind == "cursor_local":
        return has_cursor_key()
    return has_anthropic_key()


def missing_credential_message(kind: WorkerKind | None = None) -> str:
    if kind == "cursor_local":
        return "Add a Cursor API key in Settings to resume this work."
    if kind == "claude_code":
        return "Add an Anthropic API key in Settings to resume this work."
    return "Connect Cursor or add an Anthropic API key in Settings to enable background coding."
