"""
File System Plugin for JARV1S.

Always-visible sandboxed primitives for home-directory file work.
Reads/find/grep run immediately. Write/edit/move run when requested.
Delete is consent-gated and moves to OS Trash (recoverable).
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
from pathlib import Path

from core.decorators import tool
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.consent import require_consent
from core.plugins.result import ToolResult
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.plugins.ui import receipt_envelope


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


logger = logging.getLogger(__name__)

MAX_READ_BYTES = 2_000_000
MAX_LINE_CHARS = 2_000
MAX_LIST_ENTRIES = 50
MAX_SEARCH_RESULTS = 30
MAX_GREP_MATCHES = 50
SEARCH_TIMEOUT_SECS = 10
GREP_TIMEOUT_SECS = 15

# Credential-bearing names blocked even inside the home sandbox. Ordinary
# dotfiles (.zshrc, .gitconfig, configs) are allowed — only secrets are off-limits.
SECRET_NAMES: frozenset[str] = frozenset({
    ".ssh", ".gnupg", ".aws", ".docker",
    ".netrc", ".npmrc", ".pypirc",
})

_HIDDEN_NOISE: frozenset[str] = frozenset({
    ".DS_Store", ".localized", ".Spotlight-V100",
    ".Trashes", ".fseventsd", ".TemporaryItems",
    ".com.apple.timemachine.donotpresent",
    "Thumbs.db", "desktop.ini",
})

BLOCKED_PATHS: tuple[Path, ...] = (
    Path("/etc"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/System"),
    Path("/Library"),
    Path("/private"),
    Path("/dev"),
    Path("/proc"),
)

_SEARCH_EXCLUDED_DIRS: tuple[str, ...] = (
    "Library",
)


def _resolve(path: str) -> Path:
    """Resolve relative paths under home so tool behavior does not depend on backend cwd."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.home() / p
    return p.resolve()


def _fallback_search(root: Path, query: str) -> list[str]:
    matches: list[str] = []
    query_lower = query.lower()
    excluded = tuple(Path.home().resolve() / d for d in _SEARCH_EXCLUDED_DIRS)
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _: None):
        current_dir = Path(dirpath)
        if any(current_dir == ex or ex in current_dir.parents for ex in excluded):
            dirnames[:] = []
            continue
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(".") and name not in _HIDDEN_NOISE
        ]
        for name in filenames:
            visited += 1
            if visited > 20_000 or len(matches) >= MAX_SEARCH_RESULTS:
                break
            if query_lower in name.lower():
                matches.append(str(current_dir / name))
        if visited > 20_000 or len(matches) >= MAX_SEARCH_RESULTS:
            break
    return matches


def _check_sandbox(p: Path) -> CapabilityErrorDetail | None:
    """Return a typed error if `p` is outside the sandbox, or None if safe."""
    home = Path.home().resolve()

    try:
        p.relative_to(home)
    except ValueError:
        return _fail(f"Access denied: '{p}' is outside your home directory.")

    for blocked in BLOCKED_PATHS:
        try:
            p.relative_to(blocked)
            return _fail(f"Access denied: '{p}' is a protected system path.")
        except ValueError:
            pass

    for part in p.parts:
        if part in SECRET_NAMES or part.startswith(".env"):
            return _fail(f"Access denied: '{part}' holds credentials.")

    return None


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return False


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size}{unit}"
        size //= 1024
    return f"{size}TB"


def _list_directory(p: Path, offset: int, limit: int) -> str | CapabilityErrorDetail:
    try:
        entries = [
            e for e in sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
            if e.name not in _HIDDEN_NOISE
        ]
    except PermissionError:
        return _fail(f"Permission denied reading '{p}'.")

    if not entries:
        return f"'{p}' is empty."

    start = max(0, offset - 1)
    end = start + limit
    sliced = entries[start:end]
    lines = [f"Contents of {p} ({len(sliced)} of {len(entries)} items, offset={offset}):"]
    for entry in sliced:
        if entry.is_symlink():
            kind = "symlink"
            size = ""
        elif entry.is_dir():
            kind = "dir"
            size = ""
        else:
            kind = "file"
            try:
                size = f"  {_human_size(entry.stat().st_size)}"
            except OSError:
                size = ""
        lines.append(f"  [{kind}] {entry.name}{size}")

    if end < len(entries):
        lines.append(f"  ... more items. Use offset={end + 1} to continue.")
    return "\n".join(lines)


def _read_text_file(p: Path, offset: int, limit: int) -> str | CapabilityErrorDetail:
    if _is_binary(p):
        return _fail(f"Cannot read '{p.name}': binary files are not supported.")

    try:
        size = p.stat().st_size
    except OSError as e:
        return _fail(f"Cannot access '{p.name}': {e}")

    limit = max(1, min(limit, 500))
    offset = max(1, offset)

    # Oversized files: stream line-by-line so we still return the first page.
    if size > MAX_READ_BYTES:
        return _read_large_text_file(p, offset=offset, limit=limit, size=size)

    try:
        text = p.read_text(errors="replace")
    except PermissionError:
        return _fail(f"Permission denied reading '{p.name}'.")
    except OSError as e:
        return _fail(f"Could not read '{p.name}': {e}")

    return _format_text_page(p, text.splitlines(), offset=offset, limit=limit)


def _display_line(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    return f"{line[:MAX_LINE_CHARS]} [... {len(line) - MAX_LINE_CHARS} chars truncated]"


def _read_large_text_file(
    p: Path,
    *,
    offset: int,
    limit: int,
    size: int,
) -> str | CapabilityErrorDetail:
    lines: list[str] = []
    more = False
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                if i < offset:
                    continue
                if len(lines) >= limit:
                    more = True
                    break
                lines.append(_display_line(line.rstrip("\n")))
    except PermissionError:
        return _fail(f"Permission denied reading '{p.name}'.")
    except OSError as e:
        return _fail(f"Could not read '{p.name}': {e}")

    if not lines and offset > 1:
        return _fail(f"Offset {offset} is out of range.")

    end = offset + len(lines) - 1 if lines else offset - 1
    header = f"--- {p} (lines {offset}-{end}, {_human_size(size)} file) ---"
    body = "\n".join(f"{i}: {line}" for i, line in enumerate(lines, start=offset))
    suffix = ""
    if more:
        suffix = f"\n[... truncated — use offset={end + 1} to continue, or grep() to search ...]"
    else:
        suffix = f"\n[end of file — {_human_size(size)} total]"
    return f"{header}\n{body}{suffix}"


def _format_text_page(
    p: Path,
    lines: list[str],
    *,
    offset: int,
    limit: int,
) -> str:
    if offset > len(lines) and not (len(lines) == 0 and offset == 1):
        return _fail(f"Offset {offset} is out of range ({len(lines)} lines).")

    start = offset - 1
    end = start + limit
    sliced = lines[start:end]
    numbered = [f"{i}: {_display_line(line)}" for i, line in enumerate(sliced, start=offset)]
    header = f"--- {p} (lines {offset}-{offset + len(sliced) - 1 if sliced else offset - 1} of {len(lines)}) ---"
    body = "\n".join(numbered)
    suffix = ""
    if end < len(lines):
        suffix = f"\n[... truncated — use offset={end + 1} to continue ...]"
    return f"{header}\n{body}{suffix}"


async def _move_to_trash(path: Path) -> str | CapabilityErrorDetail:
    """Move a path to the OS Trash. Prefers macOS Finder; falls back to ~/.Trash."""
    if platform.system() == "Darwin":
        escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "Finder" to delete POSIX file "{escaped}"'
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            return f"Moved {path} to Trash."
        detail = (stderr or stdout).decode().strip()
        return _fail(f"Could not trash '{path.name}': {detail or f'exit {proc.returncode}'}")

    trash_dir = Path.home() / ".Trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / path.name
    if dest.exists():
        stem = path.stem
        suffix = path.suffix
        n = 1
        while dest.exists():
            dest = trash_dir / f"{stem} ({n}){suffix}"
            n += 1
    shutil.move(str(path), str(dest))
    return f"Moved {path} to Trash ({dest})."


class FilesPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="files",
        version="2.0.0",
        description="Sandboxed home-dir files: read/find/grep/edit/write/move; delete needs approval.",
        routable=False,
    )

    @tool
    async def read(self, path: str, offset: int = 1, limit: int = 200) -> str | CapabilityErrorDetail:
        """Read a text file or list a directory. Use offset/limit for large files. VOICE: summarize; do not read whole files aloud."""
        p = _resolve(path)
        err = _check_sandbox(p)
        if err:
            return err
        if not p.exists():
            return _fail(f"Not found: {p}")
        if p.is_dir():
            return _list_directory(p, offset=offset, limit=min(limit, MAX_LIST_ENTRIES))
        return _read_text_file(p, offset=offset, limit=limit)

    @tool
    async def find(self, pattern: str, path: str = "~") -> str | CapabilityErrorDetail:
        """Find files by name (default ~). Prefer over shell find/mdfind. VOICE: summarize matches."""
        root = _resolve(path)
        err = _check_sandbox(root)
        if err:
            return err
        if not root.is_dir():
            return _fail(f"'{root}' is not a directory.")

        raw_paths: list[str] = []
        if platform.system() == "Darwin":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "mdfind",
                    "-onlyin",
                    str(root),
                    f'kMDItemFSName ==[c] "*{pattern}*"',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=SEARCH_TIMEOUT_SECS)
                raw_paths = stdout.decode().strip().splitlines()
            except (asyncio.TimeoutError, OSError):
                raw_paths = []

        if not raw_paths:
            raw_paths = await asyncio.to_thread(_fallback_search, root, pattern)
        if not raw_paths:
            return f"No files matching '{pattern}' under {root}."

        home = Path.home().resolve()
        excluded = tuple(home / d for d in _SEARCH_EXCLUDED_DIRS)
        results: list[str] = []
        for p_str in raw_paths:
            p = Path(p_str).resolve()
            if _check_sandbox(p) is not None:
                continue
            if any(p == ex or ex in p.parents for ex in excluded):
                continue
            try:
                rel = p.relative_to(home)
                size = _human_size(p.stat().st_size) if p.is_file() else "dir"
                results.append(f"  ~/{rel}  ({size})")
            except (ValueError, OSError):
                continue

        if not results:
            return f"No accessible files matching '{pattern}' under {root}."

        total = len(results)
        shown = results[:MAX_SEARCH_RESULTS]
        header = f"Found {total} match{'es' if total != 1 else ''} for '{pattern}':"
        output = [header] + shown
        if total > MAX_SEARCH_RESULTS:
            output.append(f"  ... and {total - MAX_SEARCH_RESULTS} more. Narrow the path or pattern.")
        return "\n".join(output)

    @tool
    async def grep(self, pattern: str, path: str = "~", include: str | None = None) -> str | CapabilityErrorDetail:
        """Search file contents with regex. Prefer over shell grep/rg. VOICE: summarize hits."""
        root = _resolve(path)
        err = _check_sandbox(root)
        if err:
            return err
        if not root.exists():
            return _fail(f"Not found: {root}")

        cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "--max-count", "5"]
        if include:
            cmd.extend(["--glob", include])
        cmd.extend(["--", pattern, str(root)])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GREP_TIMEOUT_SECS)
        except FileNotFoundError:
            return _fail("ripgrep (rg) is not installed on this host.")
        except asyncio.TimeoutError:
            return _fail(f"grep timed out after {GREP_TIMEOUT_SECS}s. Narrow the path or pattern.")

        if proc.returncode not in (0, 1):
            detail = stderr.decode().strip()
            return _fail(f"grep failed: {detail or f'exit {proc.returncode}'}")

        raw_lines = stdout.decode(errors="replace").splitlines()
        home = Path.home().resolve()
        kept: list[str] = []
        for line in raw_lines:
            # rg format: path:line:content — path may contain colons on rare systems; split once from left for line no.
            path_part, sep, rest = line.partition(":")
            if not sep:
                continue
            try:
                p = Path(path_part).resolve()
            except OSError:
                continue
            if _check_sandbox(p) is not None:
                continue
            try:
                rel = p.relative_to(home)
                display = f"~/{rel}:{rest}"
            except ValueError:
                display = line
            kept.append(display)
            if len(kept) >= MAX_GREP_MATCHES:
                break

        if not kept:
            return f"No matches for '{pattern}' under {root}."

        header = f"Found {len(kept)} match line{'s' if len(kept) != 1 else ''} for '{pattern}'"
        if len(raw_lines) > len(kept):
            header += " (truncated)"
        header += ":"
        return "\n".join([header] + kept)

    @tool
    async def edit(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
    ) -> ToolResult | CapabilityErrorDetail:
        """Exact string replace in an existing file. Prefer over write for edits. Fails if old_text is missing or not unique (unless replace_all)."""
        if old_text == new_text:
            return _fail("old_text and new_text are identical.")
        if not old_text:
            return _fail("old_text cannot be empty. Use write() for full-file replacement.")

        p = _resolve(path)
        err = _check_sandbox(p)
        if err:
            return err
        if not p.exists():
            return _fail(f"File not found: {p}")
        if p.is_dir():
            return _fail(f"'{p.name}' is a directory.")
        if _is_binary(p):
            return _fail(f"Cannot edit '{p.name}': binary files are not supported.")

        try:
            content = p.read_text(errors="strict")
        except UnicodeDecodeError:
            return _fail(f"Cannot edit '{p.name}': not valid UTF-8 text.")
        except PermissionError:
            return _fail(f"Permission denied reading '{p}'.")
        except OSError as e:
            return _fail(f"Could not read '{p.name}': {e}")

        count = content.count(old_text)
        if count == 0:
            return _fail("old_text not found. Re-read the file and provide the exact text to replace.")
        if count > 1 and not replace_all:
            return _fail(
                f"old_text matched {count} times. Provide more context to make it unique, "
                "or set replace_all=True."
            )

        updated = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
        tmp = p.with_name(p.name + ".jarvis.tmp")
        try:
            tmp.write_text(updated)
            tmp.replace(p)
        except PermissionError:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return _fail(f"Permission denied writing '{p}'.")
        except OSError as e:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return _fail(f"Could not edit '{p.name}': {e}")

        label = f"{count} replacement{'s' if count != 1 else ''}" if replace_all else "1 replacement"
        return ToolResult(
            content=f"Edited {p} ({label}).",
            ui=[receipt_envelope("File Edited", p.name, sublabel=f"{p} · {label}")],
        )

    @tool
    async def write(self, path: str, content: str, overwrite: bool = False) -> ToolResult | CapabilityErrorDetail:
        """Create a text file. Pass overwrite=True only for intentional full replace; prefer edit for changes."""
        p = _resolve(path)
        err = _check_sandbox(p)
        if err:
            return err
        if p.exists() and not overwrite:
            return _fail(f"'{p.name}' already exists. Pass overwrite=True to replace, or use edit().")
        if p.exists() and p.is_dir():
            return _fail(f"'{p.name}' is a directory.")

        tmp = p.with_name(p.name + ".jarvis.tmp")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(content)
            tmp.replace(p)
        except PermissionError:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return _fail(f"Permission denied writing to '{p}'.")
        except OSError as e:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return _fail(f"Could not write '{p.name}': {e}")

        size = _human_size(len(content.encode()))
        return ToolResult(
            content=f"Written {p} ({size}).",
            ui=[receipt_envelope("File Written", p.name, sublabel=f"{p} · {size}")],
        )

    @tool
    async def move(self, source: str, destination: str) -> ToolResult | CapabilityErrorDetail:
        """Rename or move a file/directory. Fails if destination already exists."""
        src = _resolve(source)
        dst = _resolve(destination)

        err = _check_sandbox(src)
        if err:
            return err
        err = _check_sandbox(dst)
        if err:
            return err
        if not src.exists():
            return _fail(f"Not found: {src}")
        if dst.exists():
            return _fail(f"Destination already exists: '{dst.name}'. Choose a different name.")

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except PermissionError:
            return _fail(f"Permission denied moving '{src.name}'.")
        except OSError as e:
            return _fail(f"Could not move '{src.name}': {e}")

        return ToolResult(
            content=f"Moved '{src.name}' → '{dst.name}'.",
            ui=[receipt_envelope("File Moved", f"{src.name} → {dst.name}", sublabel=str(dst))],
        )

    @tool
    async def delete(self, path: str) -> str | CapabilityErrorDetail:
        """Delete a file/directory (moves to Trash, recoverable). Requires approval. Resolve the exact path first if vague."""
        p = _resolve(path)
        err = _check_sandbox(p)
        if err:
            return err
        if not p.exists():
            return _fail(f"Not found: {p}")

        if p.is_dir():
            try:
                count = sum(1 for _ in p.rglob("*"))
            except PermissionError:
                count = -1
            if count > 0:
                description = f"Move '{p.name}' and {count} item{'s' if count != 1 else ''} inside to Trash"
            else:
                description = f"Move empty directory '{p.name}' to Trash"
        else:
            try:
                size = _human_size(p.stat().st_size)
            except OSError:
                size = "unknown size"
            description = f"Move '{p.name}' ({size}) to Trash"

        async def _do_delete() -> str:
            try:
                return await _move_to_trash(p)
            except PermissionError:
                return _fail(f"Permission denied deleting '{p}'.")
            except OSError as e:
                return _fail(f"Could not delete '{p.name}': {e}")

        return await require_consent(description, _do_delete, detail=f"Path: {p}")
