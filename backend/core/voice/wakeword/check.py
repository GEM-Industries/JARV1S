"""Bounded wake-phrase check for setup diagnostics.

Evaluates a short PCM clip with a fresh WakeWordService so live SpeechProcessor,
STT, and turn orchestration are never involved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.voice.wakeword_service import WakeWordService

WakeCheckStatus = Literal["recognized", "not_detected", "speaker_mismatch"]
MAX_WAKE_CHECK_BYTES = 4 * 16_000 * 2


class WakeCheckError(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class WakeCheckResult:
    status: WakeCheckStatus


def validate_wake_check_pcm(pcm: bytes) -> None:
    if not isinstance(pcm, (bytes, bytearray)) or len(pcm) == 0:
        raise WakeCheckError("too_short", "Wake check clip is empty")
    if len(pcm) % 2 != 0:
        raise WakeCheckError("processing_failed", "Wake check clip is not valid PCM16")
    if len(pcm) > MAX_WAKE_CHECK_BYTES:
        raise WakeCheckError(
            "processing_failed",
            f"Wake check clip exceeds {MAX_WAKE_CHECK_BYTES} bytes",
        )


def _feed(service: WakeWordService, window: bytes) -> WakeCheckResult | None:
    if service.process(window):
        return WakeCheckResult(status="recognized")
    if not service.last_had_candidate:
        return None
    decision = service.last_decision
    if decision is not None and not decision.accept and decision.reason == "speaker_mismatch":
        return WakeCheckResult(status="speaker_mismatch")
    return None


def check_wake_phrase(pcm: bytes, *, owner_id: str | None) -> WakeCheckResult:
    validate_wake_check_pcm(pcm)
    service = WakeWordService(owner_id=owner_id)
    if not service.model_loaded:
        raise WakeCheckError("processing_failed", "Wake word model is not available")

    service.reset()
    chunk = WakeWordService.INFERENCE_WINDOW_BYTES
    offset = 0
    while offset < len(pcm):
        end = min(offset + chunk, len(pcm))
        window = bytes(pcm[offset:end])
        if len(window) < chunk:
            window = window + (b"\x00" * (chunk - len(window)))
        result = _feed(service, window)
        if result is not None:
            return result
        offset = end

    # Flush residual buffered audio with silence frames (OWW #256).
    silence = b"\x00" * chunk
    for _ in range(8):
        result = _feed(service, silence)
        if result is not None:
            return result

    return WakeCheckResult(status="not_detected")
