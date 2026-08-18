"""Wakeword cascade: Stage 1 candidate detection + Stage 2b verification."""

from core.voice.wakeword.oww_candidate import (
    INFERENCE_WINDOW_BYTES,
    INFERENCE_WINDOW_SAMPLES,
    OWWCandidateStage,
)
from core.voice.wakeword.speaker_verifier import SpeakerEmbeddingWakeVerifier
from core.voice.wakeword.types import WakeCandidate, WakeDecision, WakeStage, WakeVerifier
from core.voice.wakeword.verifiers import AcceptAllWakeVerifier

__all__ = [
    "AcceptAllWakeVerifier",
    "SpeakerEmbeddingWakeVerifier",
    "build_default_wake_verifiers",
    "INFERENCE_WINDOW_BYTES",
    "INFERENCE_WINDOW_SAMPLES",
    "OWWCandidateStage",
    "WakeCandidate",
    "WakeDecision",
    "WakeStage",
    "WakeVerifier",
]


def __getattr__(name: str):
    if name == "build_default_wake_verifiers":
        from core.voice.wakeword.factory import build_default_wake_verifiers

        return build_default_wake_verifiers
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
