"""In-memory rolling log buffer for diagnostic snapshot capture.

Captures the last N log records from the root logger. Installed at startup
via logging.getLogger().addHandler(log_buffer). The snapshot() method returns
a non-destructive copy so repeated captures see overlapping records.
"""

import logging
import re
import threading
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Iterator, TypedDict


_CONTEXT_FIELDS = ("turn_id", "instance_id", "task_id", "node_id")
_LOG_CONTEXT: ContextVar[dict[str, str]] = ContextVar("log_context", default={})
_MAX_MESSAGE_LENGTH = 8_192
_SECRET_VALUE_RE = re.compile(
    r"""(?ix)
    (
        ["']?(?:api[_-]?key|authorization|token|access[_-]?token|refresh[_-]?token|
        password|secret|credential|cookie|prompt|tool[_-]?args|owner[_-]?content)["']?
        \s*[:=]\s*
    )
    (?:
        "(?:\\.|[^"])*" |
        '(?:\\.|[^'])*' |
        [^\s,}\]]+
    )
    """
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


class LogEntry(TypedDict):
    ts: str
    level: str
    logger: str
    message: str
    context: dict[str, str]


def _safe_context_value(value: object) -> str:
    return _sanitize_control_characters(str(value))[:128]


def bind_log_context(**values: object) -> Token[dict[str, str]]:
    """Bind safe correlation values until ``reset_log_context`` is called."""
    context = _LOG_CONTEXT.get().copy()
    context.update(
        {
            key: _safe_context_value(value)
            for key, value in values.items()
            if key in _CONTEXT_FIELDS and value is not None
        }
    )
    return _LOG_CONTEXT.set(context)


def reset_log_context(token: Token[dict[str, str]]) -> None:
    _LOG_CONTEXT.reset(token)


@contextmanager
def log_context(**values: object) -> Iterator[None]:
    """Attach non-sensitive correlation identifiers to logs in this context."""
    token = bind_log_context(**values)
    try:
        yield
    finally:
        reset_log_context(token)


class LogContextFilter(logging.Filter):
    """Copy correlation identifiers from contextvars onto each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _LOG_CONTEXT.get()
        for field in _CONTEXT_FIELDS:
            if not hasattr(record, field) and field in context:
                setattr(record, field, context[field])
        return True


class HumanReadableContextFormatter(logging.Formatter):
    """Keep text logs readable while appending correlation identifiers."""

    def format(self, record: logging.LogRecord) -> str:
        original_message, original_args = record.msg, record.args
        try:
            record.msg = _sanitize_message(record.getMessage())
            record.args = ()
            rendered = super().format(record)
        finally:
            record.msg, record.args = original_message, original_args
        context = " ".join(
            f"{field}={_safe_context_value(getattr(record, field))}"
            for field in _CONTEXT_FIELDS
            if getattr(record, field, None) is not None
        )
        return f"{rendered} [{context}]" if context else rendered


def _sanitize_control_characters(value: str) -> str:
    return "".join(char if char.isprintable() else " " for char in value)


def _sanitize_message(value: str) -> str:
    value = _sanitize_control_characters(value)
    value = _BEARER_RE.sub("Bearer [REDACTED]", value)
    value = _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    if len(value) > _MAX_MESSAGE_LENGTH:
        return f"{value[:_MAX_MESSAGE_LENGTH]}…[truncated]"
    return value


class RollingLogBuffer(logging.Handler):
    """Thread-safe in-memory rolling log buffer."""

    def __init__(self, maxlen: int = 200, level: int = logging.INFO) -> None:
        super().__init__(level)
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._buffer: deque[LogEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.addFilter(LogContextFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            context = {
                field: _safe_context_value(getattr(record, field))
                for field in _CONTEXT_FIELDS
                if getattr(record, field, None) is not None
            }
            entry: LogEntry = {
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": _sanitize_message(record.getMessage()),
                "context": context,
            }
            with self._lock:
                self._buffer.append(entry)
        except Exception:
            self.handleError(record)

    def snapshot(self) -> list[LogEntry]:
        """Return a copy of all buffered records (non-destructive)."""
        with self._lock:
            return [
                {
                    **entry,
                    "context": entry["context"].copy(),
                }
                for entry in self._buffer
            ]


log_buffer = RollingLogBuffer()
