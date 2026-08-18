"""Bounded client diagnostic breadcrumbs for the satellite."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .protocol import client_diagnostics_message

_ALLOWED = frozenset(
    {
        "transport_transition",
        "mic_acquire",
        "mic_interrupted",
        "mic_flatline",
        "playback_summary",
        "playback_failed",
        "notification_failed",
    }
)
_CATEGORY = {
    "transport_transition": "transport",
    "mic_acquire": "mic",
    "mic_interrupted": "mic",
    "mic_flatline": "mic",
    "playback_summary": "playback",
    "playback_failed": "playback",
    "notification_failed": "notification",
}
_MAX_PENDING = 20
_MAX_BATCH = 10
_MAX_META_KEYS = 12
_MAX_STR = 64


def _truncate(value: str, max_len: int = _MAX_STR) -> str:
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[: max_len - 1]}…"


def _sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if len(out) >= _MAX_META_KEYS:
            break
        if not isinstance(key, str):
            continue
        clean_key = _truncate(key, 32)
        if not clean_key:
            continue
        if isinstance(value, bool) or value is None:
            out[clean_key] = value
        elif isinstance(value, int):
            out[clean_key] = value
        elif isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                continue
            out[clean_key] = round(value, 3)
        elif isinstance(value, str):
            out[clean_key] = _truncate(value)
    return out


class SatelliteDiagnostics:
    def __init__(self) -> None:
        self._seq = 0
        self._pending: list[dict[str, Any]] = []
        self._dropped = 0

    def record(
        self,
        event: str,
        *,
        severity: str = "info",
        turn_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if event not in _ALLOWED:
            return
        self._seq += 1
        entry: dict[str, Any] = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": _CATEGORY[event],
            "event": event,
            "severity": severity if severity in {"info", "warning", "error"} else "info",
            "metadata": _sanitize_metadata(metadata),
        }
        if turn_id:
            entry["turn_id"] = _truncate(turn_id)
        if len(self._pending) >= _MAX_PENDING:
            self._pending.pop(0)
            self._dropped += 1
        self._pending.append(entry)

    def next_message(self) -> dict[str, Any] | None:
        """Return the next batch without removing it."""
        if not self._pending:
            return None
        return client_diagnostics_message(
            self._pending[:_MAX_BATCH],
            dropped_count=self._dropped,
        ).as_dict()

    def mark_sent(self, count: int) -> None:
        """Remove a batch only after the WebSocket send succeeds."""
        if count <= 0:
            return
        del self._pending[:count]
        self._dropped = 0

    def drain_messages(self) -> list[dict[str, Any]]:
        """Drain all batches. Intended for tests and synchronous consumers."""
        messages: list[dict[str, Any]] = []
        while (message := self.next_message()) is not None:
            messages.append(message)
            self.mark_sent(len(message["data"]["events"]))
        return messages
