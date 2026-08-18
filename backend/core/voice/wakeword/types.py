from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class WakeCandidate:
    """Stage-1 trip: buffered audio window and score."""

    audio: bytes
    score: float
    t_start: float = 0.0
    t_end: float = 0.0
    stage: str = "oww"


@dataclass(frozen=True)
class WakeDecision:
    accept: bool
    reason: str
    speaker_id: str | None = None
    scores: dict[str, float] = field(default_factory=dict)


class WakeStage(Protocol):
    """Streaming candidate detector (Stage 0+1)."""

    def process(self, chunk: bytes) -> WakeCandidate | None: ...

    def reset(self) -> None: ...


class WakeVerifier(Protocol):
    """Precision filter run only on a candidate (Stage 2)."""

    def verify(self, candidate: WakeCandidate) -> WakeDecision: ...
