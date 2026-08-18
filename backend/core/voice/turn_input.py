"""Pure voice-input helpers extracted from the WebSocket handler layer.

These are stateless string/PCM utilities with no dependency on sessions,
``perf``, or the connection manager, so they belong in ``core.voice`` rather
than ``api.websockets``. Stateful endpoint/barge-in/fast-recovery logic stays
in the handler until characterization coverage makes a fuller seam safe.
"""

from __future__ import annotations

import re

from core.config import settings


def pcm_duration_ms(pcm_bytes_or_len: bytes | bytearray | int) -> float:
    """Duration in milliseconds for 16-bit PCM at the configured sample rate."""
    byte_count = pcm_bytes_or_len if isinstance(pcm_bytes_or_len, int) else len(pcm_bytes_or_len)
    bytes_per_second = settings.VOICE.sample_rate * settings.VOICE.channels * 2
    return round((byte_count / bytes_per_second) * 1000, 1) if bytes_per_second else 0.0


def stt_coverage_fields(
    turn_audio_bytes: int, bytes_fed: int
) -> dict[str, float | int | None]:
    """Compare captured turn PCM to bytes actually fed into the streaming STT session."""
    turn_ms = pcm_duration_ms(turn_audio_bytes)
    fed_ms = pcm_duration_ms(bytes_fed)
    gap_ms = round(max(0.0, turn_ms - fed_ms), 1)
    coverage_pct = round((bytes_fed / turn_audio_bytes) * 100, 1) if turn_audio_bytes else None
    return {
        "turn_audio_ms": turn_ms,
        "stt_bytes_fed_ms": fed_ms,
        "stt_audio_gap_ms": gap_ms,
        "stt_coverage_pct": coverage_pct,
    }


def text_tail(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    return text[-limit:] if len(text) > limit else text


def overlap_words(text: str) -> list[str]:
    return [
        word.strip("'")
        for word in re.sub(r"[^a-z0-9']+", " ", text.lower()).split()
        if word.strip("'")
    ]


def normalise_for_overlap(text: str) -> str:
    return " ".join(overlap_words(text))


def merge_continuation_text(base: str, update: str) -> str:
    """Join prior accepted text with a continuation stream, avoiding simple word overlap."""
    base = " ".join(base.split())
    update = " ".join(update.split())
    if not base:
        return update
    if not update:
        return base

    base_lower = normalise_for_overlap(base)
    update_lower = normalise_for_overlap(update)
    if update_lower.startswith(base_lower):
        return update
    if base_lower.endswith(update_lower):
        return base

    base_words = base.split()
    update_words = update.split()
    base_norm_words = overlap_words(base)
    update_norm_words = overlap_words(update)
    max_overlap = min(len(base_words), len(update_words))
    for size in range(max_overlap, 0, -1):
        if base_norm_words[-size:] == update_norm_words[:size]:
            return " ".join(base_words + update_words[size:])
    return f"{base} {update}"
