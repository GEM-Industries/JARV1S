"""Static code-worker adapters for mode=code."""

from __future__ import annotations

from plugins.agents.workers.base import (
    CodeWorker,
    CodeWorkSpec,
    EventEmitter,
    WorkerEvent,
    WorkerKind,
    WorkerResult,
    WorkerRunError,
    WorkerStartupError,
    default_worker_kind,
    lineage_worker_kind,
    missing_credential_message,
    worker_ready,
)
from plugins.agents.workers.claude import ClaudeCodeWorker

__all__ = [
    "ClaudeCodeWorker",
    "CodeWorkSpec",
    "CodeWorker",
    "EventEmitter",
    "WorkerEvent",
    "WorkerKind",
    "WorkerResult",
    "WorkerRunError",
    "WorkerStartupError",
    "default_worker_kind",
    "lineage_worker_kind",
    "missing_credential_message",
    "worker_for_kind",
    "worker_ready",
]


def worker_for_kind(kind: WorkerKind) -> CodeWorker:
    if kind == "cursor_local":
        from plugins.agents.workers.cursor import CursorLocalWorker

        return CursorLocalWorker()
    return ClaudeCodeWorker()
