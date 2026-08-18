"""Wake Stage 2b adapter over the shared enrolled-speaker verifier."""

from __future__ import annotations

from pathlib import Path

from core.voice.speaker_verifier import (
    EnrolledSpeakerVerifier,
    SpeakerMatchStatus,
)
from core.voice.wakeword.types import WakeCandidate, WakeDecision


class SpeakerEmbeddingWakeVerifier:
    """WakeVerifier adapter over EnrolledSpeakerVerifier."""

    def __init__(
        self,
        *,
        model_path: Path | None = None,
        speaker_id: str,
        threshold: float,
        profile_path: Path | None = None,
        enrollment_manifest: Path | None = None,
        num_threads: int = 1,
        enrollment_split: str = "enroll",
        verifier: EnrolledSpeakerVerifier | None = None,
    ) -> None:
        if threshold < 0.0 or threshold > 1.0:
            raise ValueError(f"Speaker threshold must be in [0, 1], got {threshold}")

        self._threshold = threshold
        self._speaker_id = speaker_id
        if verifier is not None:
            self._verifier = verifier
            return

        self._verifier = EnrolledSpeakerVerifier(
            owner_id=speaker_id,
            model_path=model_path,
            profile_path=profile_path,
            enrollment_manifest=enrollment_manifest,
            enrollment_split=enrollment_split,
            speaker_id=speaker_id,
            num_threads=num_threads,
            enabled=True,
        )

    @property
    def enrolled_verifier(self) -> EnrolledSpeakerVerifier:
        return self._verifier

    def verify(self, candidate: WakeCandidate) -> WakeDecision:
        evidence = self._verifier.verify_pcm(candidate.audio, threshold=self._threshold)
        if evidence.status is SpeakerMatchStatus.NOT_ENROLLED:
            return WakeDecision(
                accept=True,
                reason="verified",
                speaker_id=self._speaker_id,
                scores={},
            )
        if evidence.status is SpeakerMatchStatus.UNAVAILABLE:
            return WakeDecision(
                accept=False,
                reason="speaker_mismatch",
                speaker_id=self._speaker_id,
                scores={"speaker_cosine": 0.0},
            )
        return WakeDecision(
            accept=evidence.matched,
            reason="speaker_verified" if evidence.matched else "speaker_mismatch",
            speaker_id=evidence.speaker_id or self._speaker_id,
            scores={"speaker_cosine": float(evidence.cosine or 0.0)},
        )
