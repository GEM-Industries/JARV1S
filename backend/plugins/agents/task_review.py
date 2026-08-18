"""Compact task review helpers for background agent records."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, TypedDict
from uuid import uuid4


TaskSource = Literal["code", "jarvis"]


class FileSnapshot(TypedDict):
    exists: bool
    size_bytes: int | None
    mtime_ns: int | None


class TaskArtifact(TypedDict):
    path: str
    source: TaskSource
    exists_verified: bool
    exists: bool | None
    size_bytes: int | None
    changed: bool | None


class TaskActivity(TypedDict):
    source: TaskSource
    status: Literal["completed", "failed"]
    summary: str


TraceKind = Literal[
    "tool_call",
    "tool_result",
    "text",
    "reasoning",
    "ui",
    "artifact",
    "error",
    "approval_requested",
    "approval_resolved",
]
TraceStatus = Literal["running", "completed", "failed"]


class TaskTraceItem(TypedDict, total=False):
    kind: TraceKind
    ts: int
    span_id: str | None
    parent_id: str | None
    tool: str | None
    code: str | None
    args_preview: dict[str, Any] | None
    text_preview: str | None
    result_preview: str | None
    status: TraceStatus | None


_WRITTEN_PATH_RE = re.compile(r"Written:?\s+(.+?)(?:\s+\(|$)")
ACTIVITY_PREVIEW_LIMIT = 50
TRACE_PREVIEW_CHARS = 2_000
TRACE_CODE_CHARS = 8_000
TRACE_ITEM_LIMIT = 200


def file_snapshot(path: str, *, cwd: str) -> FileSnapshot:
    p = _resolve_path(path, cwd=cwd)
    try:
        if not p.is_file():
            return {"exists": False, "size_bytes": None, "mtime_ns": None}
        stat = p.stat()
        return {"exists": True, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"exists": False, "size_bytes": None, "mtime_ns": None}


def _resolve_path(path: str, *, cwd: str) -> Path:
    try:
        p = Path(path).expanduser()
    except RuntimeError:
        return Path(cwd) / path.lstrip("/")
    try:
        base = Path(cwd).expanduser()
    except RuntimeError:
        base = Path(cwd)
    if not p.is_absolute():
        p = base / p
    return p


def verify_file_artifact(
    path: str,
    *,
    cwd: str,
    source: TaskSource,
    before: FileSnapshot | None = None,
) -> TaskArtifact:
    """Return what the runtime can verify about a file path."""
    p = _resolve_path(path, cwd=cwd)
    try:
        resolved = p.resolve()
        exists = resolved.is_file()
        stat = resolved.stat() if exists else None
        size = stat.st_size if stat else None
        changed = None
        if before is not None:
            changed = (
                before["exists"] != exists
                or before["size_bytes"] != size
                or before["mtime_ns"] != (stat.st_mtime_ns if stat else None)
            )
        return {
            "path": str(resolved),
            "source": source,
            "exists_verified": True,
            "exists": exists,
            "size_bytes": size,
            "changed": changed,
        }
    except OSError:
        return {
            "path": str(p),
            "source": source,
            "exists_verified": False,
            "exists": None,
            "size_bytes": None,
            "changed": None,
        }


def merge_artifacts(existing: list[dict[str, Any]], new_items: list[TaskArtifact]) -> list[dict[str, Any]]:
    """Merge artifact records by path, keeping the latest observation."""
    merged: dict[str, dict[str, Any]] = {str(item.get("path", "")): dict(item) for item in existing}
    for item in new_items:
        merged[item["path"]] = dict(item)
    return [item for path, item in merged.items() if path]


def activity_from_tool_output(output: str, *, source: TaskSource) -> TaskActivity:
    """Create one compact activity row from an observed tool output."""
    preview = " ".join(output.split())[:180]
    return {
        "source": source,
        "status": "completed",
        "summary": preview or "Tool completed.",
    }


def append_activity(existing: list[dict[str, Any]], item: TaskActivity) -> list[dict[str, Any]]:
    return [*existing, dict(item)][-ACTIVITY_PREVIEW_LIMIT:]


def new_span_id() -> str:
    return str(uuid4())


def preview_text(value: Any, *, limit: int = TRACE_PREVIEW_CHARS) -> str:
    text = value if isinstance(value, str) else str(value)
    compact = " ".join(text.split())
    return compact[:limit]


def preview_args(value: Any, *, limit: int = TRACE_PREVIEW_CHARS) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    preview: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (str, int, float, bool)) or item is None:
            preview[key] = preview_text(item, limit=limit) if isinstance(item, str) else item
        elif isinstance(item, list):
            # Preserve structured invocation ledgers without flattening them away.
            if key == "invocations" and all(isinstance(v, dict) for v in item):
                preview[key] = item[:20]
            else:
                preview[key] = [preview_text(v, limit=300) for v in item[:5]]
        elif isinstance(item, dict):
            preview[key] = {
                str(k): preview_text(v, limit=300)
                for k, v in list(item.items())[:10]
            }
        else:
            preview[key] = preview_text(item, limit=300)
    return preview


def task_trace_item(
    *,
    kind: TraceKind,
    ts: int,
    span_id: str | None = None,
    parent_id: str | None = None,
    tool: str | None = None,
    code: str | None = None,
    args_preview: dict[str, Any] | None = None,
    text_preview: str | None = None,
    result_preview: str | None = None,
    status: TraceStatus | None = None,
) -> TaskTraceItem:
    item: TaskTraceItem = {
        "kind": kind,
        "ts": ts,
        "span_id": span_id,
        "parent_id": parent_id,
        "tool": tool,
        "status": status,
    }
    if code is not None:
        item["code"] = code[:TRACE_CODE_CHARS]
    if args_preview is not None:
        item["args_preview"] = args_preview
    if text_preview is not None:
        item["text_preview"] = preview_text(text_preview)
    if result_preview is not None:
        item["result_preview"] = preview_text(result_preview)
    return item


def append_trace(existing: list[dict[str, Any]], item: TaskTraceItem) -> list[dict[str, Any]]:
    return [*existing, dict(item)][-TRACE_ITEM_LIMIT:]


def format_tool_result_content(content: Any) -> str:
    """Flatten SDK tool result payloads for trace previews."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            parts.append(text if isinstance(text, str) else str(block))
        return "\n".join(parts)
    return str(content)


def written_artifacts_from_output(output: str, *, cwd: str, source: TaskSource) -> list[TaskArtifact]:
    """Extract file artifacts from tool success output and verify path existence."""
    artifacts: list[TaskArtifact] = []
    for raw_path in _WRITTEN_PATH_RE.findall(output):
        artifacts.append(verify_file_artifact(raw_path.strip(), cwd=cwd, source=source))
    return artifacts
