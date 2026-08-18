"""Capability-first allow / ask / deny policy for jarvis.system.exec.

Commands run by default. A short list of destructive operations and protected
paths is denied, while a few consequential mutations require confirmation.
"""

from __future__ import annotations

import shlex
from enum import Enum
from pathlib import Path


class ExecVerdict(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


DENY_SUBSTRINGS: tuple[str, ...] = (
    ":(){ :|:& };:",
    "> /dev/",
    "chmod -r 777",
    "dd if=",
    "mkfs",
    "reboot",
    "rm -rf",
    "shutdown",
    "sudo",
)

SECRET_PATH_MARKERS: tuple[str, ...] = (
    "/.ssh/",
    "/.gnupg/",
    "/.aws/",
    "/.netrc",
    "/.npmrc",
    "/.pypirc",
    "/id_rsa",
    "/id_ed25519",
    "/id_ecdsa",
    "/credentials",
    "/.env",
)

_SHELL_BINS = frozenset({"bash", "sh", "zsh"})
_DOWNLOAD_BINS = frozenset({"curl", "wget"})


def _looks_like_secret_path(text: str) -> bool:
    expanded = text.replace("~", str(Path.home()))
    lower = expanded.lower()
    return any(marker in lower for marker in SECRET_PATH_MARKERS)


def _tokenize(command: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _is_remote_pipe_to_shell(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token != "|":
            continue
        before = {Path(item).name for item in tokens[:index]}
        after = tokens[index + 1 : index + 2]
        if before & _DOWNLOAD_BINS and after and Path(after[0]).name in _SHELL_BINS:
            return True
    return False


def _ask_reason(tokens: list[str]) -> str | None:
    if not tokens:
        return None
    executable = Path(tokens[0]).name
    if executable in {"rm", "rmdir"}:
        return f"File removal requires approval: '{executable}'"
    if executable == "git" and len(tokens) >= 2 and tokens[1] == "push":
        return "Publishing git changes requires approval."
    if executable == "diskutil" and any(arg.startswith("erase") for arg in tokens[1:]):
        return "Erasing a disk requires approval."
    return None


def classify_exec(command: str) -> tuple[ExecVerdict, str]:
    """Return (verdict, reason) for a shell command string."""
    raw = command.strip()
    if not raw:
        return ExecVerdict.DENY, "Empty command."

    lower = raw.lower()
    for sub in DENY_SUBSTRINGS:
        if sub in lower:
            return ExecVerdict.DENY, f"Blocked pattern: {sub}"

    if _looks_like_secret_path(raw):
        return ExecVerdict.DENY, "Blocked: command references a protected secrets path."

    tokens = _tokenize(raw)
    if tokens is None:
        return ExecVerdict.ASK, "Could not parse command quoting."

    if _is_remote_pipe_to_shell(tokens):
        return ExecVerdict.DENY, "Blocked: piping downloaded code into a shell."

    command_start = 0
    for index, token in enumerate([*tokens, ";"]):
        if token not in {"|", "||", "&&", ";", "&"}:
            continue
        segment = tokens[command_start:index]
        command_start = index + 1
        if reason := _ask_reason(segment):
            return ExecVerdict.ASK, reason

    return ExecVerdict.ALLOW, "Allowed by capability-first policy."
