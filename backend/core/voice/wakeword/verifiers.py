from __future__ import annotations

from core.voice.wakeword.types import WakeCandidate, WakeDecision


class AcceptAllWakeVerifier:
    """Behavior-preserving no-op verifier for pipeline bring-up."""

    def verify(self, candidate: WakeCandidate) -> WakeDecision:
        return WakeDecision(
            accept=True,
            reason="accept_all",
            scores={candidate.stage: candidate.score},
        )
