"""Shared env helpers for Home Assistant setup CLIs."""

from __future__ import annotations

import re
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_SECRET_KEYS = {"HA_TOKEN"}


def env_path() -> Path:
    return _ENV_PATH


def display_env_line(key: str, value: str) -> str:
    if key in _SECRET_KEYS:
        return f"{key}=<redacted>"
    return f"{key}={value}"


def upsert_env(key: str, value: str) -> None:
    line = f"{key}={value}"
    display_line = display_env_line(key, value)
    if not _ENV_PATH.exists():
        _ENV_PATH.write_text(f"{line}\n", encoding="utf-8")
        print(f"Created {_ENV_PATH} and wrote: {display_line}")
        return

    content = _ENV_PATH.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    if pattern.search(content):
        updated = pattern.sub(line, content)
        _ENV_PATH.write_text(updated, encoding="utf-8")
        print(f"Updated {_ENV_PATH}: {display_line}")
    else:
        separator = "\n" if content and not content.endswith("\n") else ""
        _ENV_PATH.write_text(content + separator + line + "\n", encoding="utf-8")
        print(f"Appended to {_ENV_PATH}: {display_line}")


def read_env_value(key: str) -> str | None:
    if not _ENV_PATH.exists():
        return None
    match = re.search(rf"^{re.escape(key)}\s*=\s*(.*)$", _ENV_PATH.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None
