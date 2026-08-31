"""Named-work lineage over background_tasks runs.

A run is `task_id`. Work is `work_id` + title and stays open after a run
completes. Resolution is exact id, then unique title/ref/cwd among open work.
Ambiguous targets return candidates and do not guess.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from services.database.mongodb import mongodb

TASK_TTL = timedelta(days=30)
OPEN_WORK_LIMIT = 24
OPEN_FETCH_LIMIT = 50
OWNER_FETCH_LIMIT = 80
ROSTER_LIMIT = 5
FOLDER_LIST_LIMIT = 8
PROJECT_ROOT_NAMES = ("dev", "Developer", "src", "code", "projects")
PROJECT_SCAN_LIMIT = 24
FOLDER_CACHE_TTL_SEC = 30.0
_TASK_PROJECTION = {
    "events": 0,
    "trace": 0,
    "activity": 0,
    "artifacts": 0,
    "mcp_servers": 0,
    "_id": 0,
}
_STOPWORDS = frozenset({"it", "that", "this", "the", "work", "task", "job", "one"})
_GENERIC_STEER = frozenset({"tweak", "continue", "resume", "update", "fix", "steer", "change"})
_THE_PREFIX = re.compile(r"^the\s+")
_HASH_TICKET = re.compile(r"(?:#|pr\s*#?)\s*(\d{3,6})", re.I)
_REVIEW_TICKET = re.compile(r"\b(\d{3,6})\s+review\b", re.I)

ResolveStatus = Literal["none", "single", "ambiguous"]
_root_scan: tuple[float, tuple[str, ...]] = (0.0, ())


@dataclass(frozen=True)
class WorkResolve:
    status: ResolveStatus
    doc: dict[str, Any] | None = None
    candidates: tuple[dict[str, Any], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class FolderResolve:
    status: ResolveStatus
    path: str | None = None
    candidates: tuple[str, ...] = ()
    reason: str = ""


def ttl_at(now: datetime | None = None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    return stamp + TASK_TTL


def is_open(doc: dict[str, Any]) -> bool:
    return doc.get("open") is True


def lineage_key(doc: dict[str, Any]) -> str:
    return str(doc.get("work_id") or doc.get("task_id") or "")


def display_cwd(cwd: str | None) -> str:
    raw = str(cwd or "").strip()
    if not raw:
        return ""
    home = os.path.expanduser("~")
    if raw == home or raw.startswith(home + os.sep):
        return "~" + raw[len(home):]
    return raw


def normalize_target(value: str | None) -> str:
    text = _THE_PREFIX.sub("", str(value or "").strip().lower())
    text = re.sub(r"[#]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def cwd_basename(cwd: str | None) -> str:
    raw = str(cwd or "").rstrip("/").strip()
    return os.path.basename(raw).lower() if raw else ""


def latest_per_work_id(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest run per lineage. Input should be newest-first."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for doc in docs:
        key = lineage_key(doc)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(doc)
    return out


def _touched_at(doc: dict[str, Any]) -> datetime:
    raw = doc.get("created_at") or doc.get("completed_at")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        ts = raw / 1000.0 if raw > 1e12 else float(raw)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime.min.replace(tzinfo=timezone.utc)


def recency_roster(docs: list[dict[str, Any]], *, limit: int = ROSTER_LIMIT) -> list[dict[str, Any]]:
    """Running first, then newest lineages. Prompt-sized; close is not required."""
    open_docs = [doc for doc in docs if is_open(doc)]
    open_docs.sort(key=_touched_at, reverse=True)
    latest = latest_per_work_id(open_docs)
    running = [doc for doc in latest if doc.get("status") == "running"]
    rest = [doc for doc in latest if doc.get("status") != "running"]
    return (running + rest)[:limit]


def resolve_steer(docs: list[dict[str, Any]]) -> WorkResolve:
    window = recency_roster(docs)
    if len(window) == 1:
        return WorkResolve("single", doc=window[0])
    if len(window) > 1:
        return WorkResolve("ambiguous", candidates=tuple(window), reason="ambiguous_pronoun")
    return WorkResolve("none", reason="not_found")


def list_project_dirs(extra: list[str] | None = None, *, limit: int = PROJECT_SCAN_LIMIT) -> list[str]:
    """Direct children of ~/dev and similar — Claude sessions are cwd-tied; nicknames resolve here."""
    scored: list[tuple[float, str]] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        real = os.path.abspath(os.path.expanduser(path))
        if real in seen or not os.path.isdir(real):
            return
        seen.add(real)
        try:
            mtime = os.path.getmtime(real)
        except OSError:
            mtime = 0.0
        scored.append((mtime, real))

    for path in extra or []:
        if path:
            _add(str(path))
    for path in _scan_project_roots():
        _add(path)
    scored.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in scored[:limit]]


def _scan_project_roots() -> tuple[str, ...]:
    global _root_scan
    now = time.monotonic()
    cached_at, cached = _root_scan
    if cached and now - cached_at < FOLDER_CACHE_TTL_SEC:
        return cached

    paths: list[str] = []
    home = os.path.expanduser("~")
    for name in PROJECT_ROOT_NAMES:
        root = os.path.join(home, name)
        if not os.path.isdir(root):
            continue
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                        continue
                    paths.append(entry.path)
        except OSError:
            continue
    cached = tuple(paths)
    _root_scan = (now, cached)
    return cached


def resolve_folder(needle: str | None, known: list[str]) -> FolderResolve:
    raw = str(needle or "").strip()
    if raw:
        expanded = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(expanded):
            return FolderResolve("single", path=expanded)
    text = normalize_target(raw)
    if not text or text in _STOPWORDS or len(text) < 3:
        return FolderResolve("none", reason="missing")

    catalog = [(path, normalize_target(cwd_basename(path))) for path in known if cwd_basename(path)]
    exact = [path for path, name in catalog if name == text]
    if len(exact) == 1:
        return FolderResolve("single", path=exact[0])
    if len(exact) > 1:
        return FolderResolve("ambiguous", candidates=tuple(exact), reason="ambiguous_folder")

    hits: list[str] = []
    for path, name in catalog:
        if text in name or (len(name) >= 4 and name in text):
            hits.append(path)
            continue
        parts = [part for part in re.split(r"[-_\s]+", name) if part]
        suffix = "-".join(parts[-2:]) if len(parts) >= 2 else ""
        if len(suffix) >= 4 and suffix in text:
            hits.append(path)
            continue
        tokens = [part for part in parts if len(part) >= 4][:2]
        if tokens and all(part in text for part in tokens):
            hits.append(path)
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return FolderResolve("single", path=unique[0])
    if len(unique) > 1:
        return FolderResolve("ambiguous", candidates=tuple(unique), reason="ambiguous_folder")
    return FolderResolve("none", reason="not_found")


def infer_title(title: str | None, cwd: str | None, prompt: str) -> str:
    named = str(title or "").strip()
    if named:
        return named[:80]
    text = str(prompt or "")
    hashed = _HASH_TICKET.search(text)
    if hashed:
        return f"{hashed.group(1)} review"
    reviewed = _REVIEW_TICKET.search(text)
    if reviewed:
        return f"{reviewed.group(1)} review"
    base = cwd_basename(cwd)
    if base:
        return base[:80]
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    return (line[:80] if line else "Untitled")


def _looks_like_steer(prompt: str | None) -> bool:
    text = normalize_target(prompt)
    tokens = [token for token in text.split() if token not in _STOPWORDS]
    if not tokens or set(tokens) <= _GENERIC_STEER:
        return True
    if any("/" in token or token.startswith("~") for token in tokens):
        return False
    if any("-" in token and len(token) >= 4 for token in tokens):
        return False
    return len(tokens) <= 10


def match_open_work(
    docs: list[dict[str, Any]],
    *,
    title: str | None = None,
    cwd: str | None = None,
    prompt: str | None = None,
) -> WorkResolve:
    """Find existing open work. Short constraints with no folder continue unique open work."""
    for hint in (title, cwd, cwd_basename(cwd)):
        if hint and str(hint).strip():
            got = resolve_from_docs(docs, str(hint))
            if got.status != "none":
                return got
    if prompt and str(prompt).strip():
        got = resolve_from_docs(docs, prompt)
        if got.status != "none":
            return got
    if not (title or cwd) and _looks_like_steer(prompt):
        return resolve_steer(docs)
    return WorkResolve("none", reason="not_found")


def inspect_resume_command(cwd: str, session_id: str, binary: str) -> str:
    return f"cd {shlex.quote(cwd)} && {binary} --resume {shlex.quote(session_id)}"


def inspect_launch_argv(cwd: str, session_id: str, *, binary: str) -> list[str]:
    command = inspect_resume_command(cwd, session_id, binary)
    return [
        "osascript",
        "-e",
        f'tell application "Terminal" to do script {json.dumps(command)}',
        "-e",
        'tell application "Terminal" to activate',
    ]


INSPECT_OPEN_LIMIT = 12


def path_under_cwd(path: str, cwd: str) -> str | None:
    """Resolve path against cwd and reject anything outside the project folder."""
    raw_path = str(path or "").strip()
    raw_cwd = str(cwd or "").strip()
    if not raw_path or not raw_cwd:
        return None
    try:
        base = Path(raw_cwd).expanduser().resolve()
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved = candidate.resolve()
        resolved.relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return None
    return str(resolved)


def inspect_ide_argv(binary: str, targets: Sequence[str]) -> list[str]:
    return [binary, "--reuse-window", *[str(target) for target in targets if target][:INSPECT_OPEN_LIMIT]]


def inspect_macos_open_argv(app: str, targets: Sequence[str]) -> list[str]:
    return ["open", "-a", app, *[str(target) for target in targets if target][:INSPECT_OPEN_LIMIT]]


def format_cwd_help(known: list[str]) -> str:
    if not known:
        return (
            "mode=code needs a project folder. Ask which repo. "
            "Do not default to the JARV1S tree."
        )
    listed = ", ".join(display_cwd(path) for path in known[:FOLDER_LIST_LIMIT])
    return f"mode=code needs a project folder. Known folders: {listed}."


def format_known_folders(paths: list[str]) -> str:
    lines = [f"- {display_cwd(path)}" for path in paths[:FOLDER_LIST_LIMIT]]
    if not lines:
        return ""
    return (
        "[PROJECT FOLDERS]\n"
        + "\n".join(lines)
        + "\nPass cwd as the folder name. Do not files.find these."
    )


def _haystacks(doc: dict[str, Any]) -> list[str]:
    fields = [
        normalize_target(str(doc.get("title") or "")),
        normalize_target(str(doc.get("ref") or "")),
        cwd_basename(doc.get("cwd")),
    ]
    return [item for item in fields if item]


def _title_matches(doc: dict[str, Any], needle: str) -> bool:
    if not needle or needle in _STOPWORDS or len(needle) < 3:
        return False
    for hay in _haystacks(doc):
        if needle == hay or needle in hay or (len(hay) >= 4 and hay in needle):
            return True
    return False


def resolve_from_docs(docs: list[dict[str, Any]], target: str | None) -> WorkResolve:
    needle = normalize_target(target)
    if not needle:
        return WorkResolve("none", reason="missing_target")

    by_task = [doc for doc in docs if str(doc.get("task_id") or "") == str(target).strip()]
    if len(by_task) == 1:
        return WorkResolve("single", doc=by_task[0])

    work_id = str(target).strip()
    by_work = [doc for doc in docs if str(doc.get("work_id") or "") == work_id]
    if by_work:
        latest = latest_per_work_id(by_work)
        return WorkResolve("single", doc=latest[0])

    tokens = [token for token in needle.split() if token not in _STOPWORDS]
    if not tokens or set(tokens) <= _GENERIC_STEER:
        return resolve_steer(docs)

    open_latest = latest_per_work_id([doc for doc in docs if is_open(doc)])
    hits = [doc for doc in open_latest if _title_matches(doc, " ".join(tokens))]
    if len(hits) == 1:
        return WorkResolve("single", doc=hits[0])
    if len(hits) > 1:
        return WorkResolve("ambiguous", candidates=tuple(hits), reason="ambiguous_title")
    return WorkResolve("none", reason="not_found")


def format_roster_line(doc: dict[str, Any]) -> str:
    title = str(doc.get("title") or doc.get("task_id") or "Untitled").strip()
    status = str(doc.get("status") or "unknown")
    cwd = display_cwd(doc.get("cwd"))
    path = f", {cwd}" if cwd else ""
    return f"- {title} — {status}{path}"


def format_roster(docs: list[dict[str, Any]], known_folders: list[str] | None = None) -> str:
    lines = [format_roster_line(doc) for doc in recency_roster(docs)]
    parts: list[str] = []
    if lines:
        parts.append(
            "[OPEN WORK]\n"
            + "\n".join(lines)
            + "\nBy title: resume to continue, inspect to read. "
            "close forgets; not required when a run finishes. "
            "Do not recall(). House talk must not resume or dispatch this work."
        )
    folders = format_known_folders(known_folders or [])
    if folders:
        parts.append(folders)
    return "\n".join(parts)


def format_status(doc: dict[str, Any]) -> str:
    title = str(doc.get("title") or doc.get("task_id") or "Untitled").strip()
    status = str(doc.get("status") or "unknown")
    cwd = display_cwd(doc.get("cwd"))
    progress = str(doc.get("progress_summary") or "").strip()
    result_line = str(doc.get("result") or progress).strip().splitlines()[0][:180]
    open_bit = is_open(doc)
    parts = [
        f"{title} — {status}"
        + (", open" if open_bit and status != "running" else "")
        + (f", {cwd}" if cwd else ""),
    ]
    if status == "running" and progress:
        parts.append(f"progress={progress}")
    elif result_line:
        parts.append(f"last result: {result_line}")
    if open_bit and status in {"completed", "failed", "cancelled"}:
        parts.append(
            "Still open — resume to continue, inspect to read it. "
            "Do not recall() or narrate the transcript."
        )
    return "\n".join(parts)


def format_candidates(candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> str:
    if not candidates:
        return "No matching open work."
    lines = ["Ambiguous — which one?"]
    lines.extend(format_roster_line(doc) for doc in candidates)
    return "\n".join(lines)


def compact_task(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": doc.get("task_id"),
        "work_id": doc.get("work_id"),
        "title": doc.get("title"),
        "ref": doc.get("ref"),
        "open": doc.get("open"),
        "status": doc.get("status"),
        "mode": doc.get("mode"),
        "cwd": doc.get("cwd"),
        "progress_summary": doc.get("progress_summary"),
        "session_id": doc.get("session_id"),
        "worker_kind": doc.get("worker_kind"),
        "created_at": doc.get("created_at"),
        "completed_at": doc.get("completed_at"),
    }


async def _load_tasks(
    owner_id: str,
    *,
    open_only: bool = False,
    limit: int,
) -> list[dict[str, Any]]:
    col = mongodb.get_collection("background_tasks")
    filt: dict[str, Any] = {"owner_id": owner_id}
    if open_only:
        filt["open"] = True
    cursor = col.find(filt, _TASK_PROJECTION).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def list_open_work(owner_id: str, limit: int = OPEN_WORK_LIMIT) -> list[dict[str, Any]]:
    docs = await _load_tasks(owner_id, open_only=True, limit=OPEN_FETCH_LIMIT)
    return latest_per_work_id(docs)[:limit]


async def load_open_roster(owner_id: str) -> str:
    docs = await list_open_work(owner_id)
    known = list_project_dirs([str(doc.get("cwd") or "") for doc in docs])
    return format_roster(docs, known)


async def load_owner_docs(owner_id: str) -> list[dict[str, Any]]:
    return await _load_tasks(owner_id, limit=OWNER_FETCH_LIMIT)


async def resolve_target(owner_id: str, target: str | None) -> WorkResolve:
    return resolve_from_docs(await load_owner_docs(owner_id), target)
