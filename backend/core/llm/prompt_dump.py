"""Prompt dump utility for LLM context auditing.

When PROMPT_DUMP_ENABLED=true, writes the full message array (with per-message
token estimates) to logs/prompt_dumps/ before each LLM call. Use this to audit
token usage, identify tool-schema bloat, and measure context growth across turns.

File naming: YYYYMMDD_HHMMSS_<counter>_<tag>.txt
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

_counter = 0
_lock = Lock()

_DUMP_DIR: Path | None = None
_DEFAULT_MAX_DUMPS = 100
_DEFAULT_MAX_AGE_DAYS = 7


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _cleanup_prompt_dumps(
    dump_dir: Path,
    *,
    max_files: int | None = None,
    max_age_days: int | None = None,
) -> None:
    """Remove expired and excess prompt dumps without traversing other directories."""
    max_files = max_files or _positive_env_int(
        "JARVIS_PROMPT_DUMP_MAX_FILES", _DEFAULT_MAX_DUMPS
    )
    max_age_days = max_age_days or _positive_env_int(
        "JARVIS_PROMPT_DUMP_MAX_AGE_DAYS", _DEFAULT_MAX_AGE_DAYS
    )
    cutoff = time.time() - (max_age_days * 24 * 60 * 60)
    retained: list[tuple[float, Path]] = []

    for path in dump_dir.glob("*.txt"):
        try:
            modified = path.stat().st_mtime
            if modified < cutoff:
                path.unlink()
            else:
                retained.append((modified, path))
        except OSError as error:
            logger.debug("Could not clean prompt dump %s: %s", path.name, error)

    retained.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    for _, path in retained[max_files:]:
        try:
            path.unlink()
        except OSError as error:
            logger.debug("Could not remove excess prompt dump %s: %s", path.name, error)


def _get_dump_dir() -> Path:
    global _DUMP_DIR
    if _DUMP_DIR is None:
        from core.config import settings
        _DUMP_DIR = settings.LOGS_DIR / "prompt_dumps"
        _DUMP_DIR.mkdir(parents=True, exist_ok=True)
    return _DUMP_DIR


def _est_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return max(1, len(text) // 4)


def _role_label(role: str) -> str:
    return {"system": "SYSTEM", "user": "USER", "assistant": "ASSISTANT"}.get(role, role.upper())


def _render_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    parts.append("[IMAGE]")
                else:
                    parts.append(json.dumps(p))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(content)


def dump_prompt(
    messages: list[dict],
    model: str,
    tag: str = "",
) -> None:
    """Write the full message array to a prompt dump file.

    Called by LLMService before each API call when PROMPT_DUMP_ENABLED is set.
    Thread-safe via a module-level counter lock.
    """
    global _counter
    try:
        with _lock:
            _counter += 1
            call_num = _counter
            dump_dir = _get_dump_dir()
            _cleanup_prompt_dumps(dump_dir)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = f"{ts}_{call_num:04d}"
            if tag:
                safe_tag = "".join(
                    c if c.isalnum() or c in "-_" else "_" for c in tag
                )[:40]
                slug = f"{slug}_{safe_tag}"
            out_path = dump_dir / f"{slug}.txt"

            lines: list[str] = []

            # ── Header ──────────────────────────────────────────────────────
            lines += [
                "=" * 80,
                f"PROMPT DUMP #{call_num}",
                f"Time  : {datetime.now().isoformat()}",
                f"Model : {model}",
                f"Msgs  : {len(messages)}",
                "=" * 80,
                "",
            ]

            # ── Per-message breakdown ────────────────────────────────────────
            total_tokens = 0
            token_by_role: dict[str, int] = {}

            for i, msg in enumerate(messages):
                role = msg.get("role", "?")
                raw_content = msg.get("content", "")
                rendered = _render_content(raw_content)
                tok = _est_tokens(rendered)
                total_tokens += tok
                token_by_role[role] = token_by_role.get(role, 0) + tok

                label = _role_label(role)
                sep = "─" * 80
                lines += [
                    sep,
                    f"[{i}] {label}  (~{tok:,} tokens)",
                    sep,
                    rendered,
                    "",
                ]

            # ── Summary ─────────────────────────────────────────────────────
            lines += [
                "=" * 80,
                "TOKEN SUMMARY (estimated, ~4 chars/token)",
                "=" * 80,
            ]
            for role, tok in sorted(token_by_role.items()):
                lines.append(f"  {role:<12} {tok:>8,} tokens")
            lines += [
                f"  {'TOTAL':<12} {total_tokens:>8,} tokens",
                "",
            ]

            # ── System prompt section breakdown ─────────────────────────────
            if messages and messages[0].get("role") == "system":
                sys_content = _render_content(messages[0].get("content", ""))
                lines += [
                    "=" * 80,
                    "SYSTEM PROMPT SECTION BREAKDOWN",
                    "=" * 80,
                ]
                sections = [s.strip() for s in sys_content.split("\n\n") if s.strip()]
                for j, sec in enumerate(sections):
                    first_line = sec.splitlines()[0][:100]
                    sec_tok = _est_tokens(sec)
                    lines.append(f"  §{j:<3} {sec_tok:>6,} tokens  |  {first_line}")
                lines.append("")

            out_path.write_text("\n".join(lines), encoding="utf-8")
            _cleanup_prompt_dumps(dump_dir)
        logger.debug("Prompt dump written: %s  (~%d tokens)", out_path.name, total_tokens)

    except Exception as e:
        logger.warning("prompt_dump failed: %s", e)
