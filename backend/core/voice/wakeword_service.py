import logging
from pathlib import Path
from typing import Optional

from core.config import settings
from core.voice.speaker_verifier import EnrolledSpeakerVerifier
from core.voice.wakeword.factory import build_default_wake_verifiers
from core.voice.wakeword.oww_candidate import (
    INFERENCE_WINDOW_BYTES,
    INFERENCE_WINDOW_SAMPLES,
    OWWCandidateStage,
)
from core.voice.wakeword.types import WakeCandidate, WakeDecision, WakeVerifier

logger = logging.getLogger(__name__)


class WakeWordService:
    """
    Orchestrates wakeword detection: candidate stage + verifier chain.
    Preserves the legacy process()/reset()/reload()/consume_detection_audio() surface.
    """

    INFERENCE_WINDOW_SAMPLES = INFERENCE_WINDOW_SAMPLES
    BYTES_PER_SAMPLE = 2
    INFERENCE_WINDOW_BYTES = INFERENCE_WINDOW_BYTES

    def __init__(
        self,
        model_path: str = "resources/models/wakeword/Jarvis.onnx",
        *,
        owner_id: str | None = None,
        sensitivity: float | None = None,
        consecutive_required: int | None = None,
        vad_threshold: float | None = None,
        verifiers: list[WakeVerifier] | None = None,
        speaker_verifier: EnrolledSpeakerVerifier | None = None,
    ):
        self.owner_id = owner_id
        if verifiers is None and speaker_verifier is None:
            speaker_verifier = EnrolledSpeakerVerifier(owner_id=owner_id)
        self.speaker_verifier = speaker_verifier
        self.sensitivity: float = (
            sensitivity if sensitivity is not None else settings.VOICE.wakeword_sensitivity
        )
        self.consecutive_required: int = max(
            1,
            consecutive_required
            if consecutive_required is not None
            else settings.VOICE.wakeword_patience,
        )
        self.vad_threshold: float = (
            vad_threshold if vad_threshold is not None else settings.VOICE.wakeword_vad_threshold
        )
        self._injected_verifiers = verifiers is not None
        self._verifiers: list[WakeVerifier] = (
            verifiers
            if verifiers is not None
            else build_default_wake_verifiers(owner_id, speaker_verifier=speaker_verifier)
        )
        self._last_detection_audio: Optional[bytes] = None
        self._last_decision: WakeDecision | None = None
        self._last_had_candidate: bool = False
        self._stats = {"candidates": 0, "verifier_rejects": 0, "commits": 0}
        self._model_name: Optional[str] = None

        self._model_path = Path(__file__).parent.parent.parent / model_path
        self._candidate_stage = OWWCandidateStage(
            self._model_path,
            sensitivity=self.sensitivity,
            consecutive_required=self.consecutive_required,
            vad_threshold=self.vad_threshold,
        )
        self._model_loaded = self._candidate_stage.model_loaded
        self._model_name = self._candidate_stage._model_name  # noqa: SLF001
        self.vad_threshold = self._candidate_stage.vad_threshold

        if self._model_loaded:
            logger.info(
                "Wake Word Service initialized | model=%s sensitivity=%.2f consecutive=%d vad_threshold=%.2f owner=%s",
                self._model_name,
                self.sensitivity,
                self.consecutive_required,
                self.vad_threshold,
                self.owner_id,
            )

    @property
    def inference_count(self) -> int:
        return self._candidate_stage.inference_count

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded

    def reload(self) -> None:
        """Hot-swap the ONNX model from disk after a retrain without restarting the backend."""
        logger.info("Reloading wake word model from disk...")
        self._model_loaded = False
        self._last_detection_audio = None
        self._last_decision = None
        self._candidate_stage.reload()
        self._model_loaded = self._candidate_stage.model_loaded
        self._model_name = self._candidate_stage._model_name  # noqa: SLF001
        self.vad_threshold = self._candidate_stage.vad_threshold

    def reload_verifiers(self) -> None:
        """Rebuild the Stage 2b verifier chain from the current owner profile."""
        if self._injected_verifiers:
            logger.debug("Skipping verifier reload for injected test verifiers")
            return
        if self.speaker_verifier is not None:
            self.speaker_verifier.reload_profile()
        self._verifiers = build_default_wake_verifiers(
            self.owner_id,
            speaker_verifier=self.speaker_verifier,
        )
        self._last_decision = None
        logger.info("Reloaded wake verifiers | owner=%s count=%d", self.owner_id, len(self._verifiers))

    def reset(self) -> None:
        """Flush model buffers after detection or suppression gap (openWakeWord #256)."""
        self._candidate_stage.reset()
        self._last_had_candidate = False

    def reset_stats(self) -> None:
        """Clear pipeline attribution counters (eval harness)."""
        self._stats = {"candidates": 0, "verifier_rejects": 0, "commits": 0}

    @property
    def pipeline_stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def last_had_candidate(self) -> bool:
        """True if the most recent process() call emitted a Stage-1 candidate."""
        return self._last_had_candidate

    def process(self, chunk: bytes) -> bool:
        """Process incoming audio. Returns True only on committed wake (post-verifier)."""
        if not self._model_loaded:
            return False

        candidate = self._candidate_stage.process(chunk)
        self._last_had_candidate = candidate is not None
        if candidate is None:
            return False

        self._stats["candidates"] += 1
        decision = self._verify(candidate)
        self._last_decision = decision
        if not decision.accept:
            self._stats["verifier_rejects"] += 1
            self._candidate_stage.reset()
            logger.debug("Wake candidate rejected | reason=%s scores=%s", decision.reason, decision.scores)
            return False

        self._stats["commits"] += 1

        self._last_detection_audio = candidate.audio
        logger.info(
            "Wake Word Detected! (%s: %.4f, reason=%s)",
            self._model_name,
            candidate.score,
            decision.reason,
        )
        return True

    def _verify(self, candidate: WakeCandidate) -> WakeDecision:
        scores: dict[str, float] = {candidate.stage: candidate.score}
        for verifier in self._verifiers:
            decision = verifier.verify(candidate)
            scores.update(decision.scores)
            if not decision.accept:
                return WakeDecision(
                    accept=False,
                    reason=decision.reason,
                    speaker_id=decision.speaker_id,
                    scores=scores,
                )
        return WakeDecision(accept=True, reason="verified", scores=scores)

    def consume_detection_audio(self) -> Optional[bytes]:
        """Return captured audio from the last committed wake and clear it."""
        audio = self._last_detection_audio
        self._last_detection_audio = None
        return audio

    @property
    def last_decision(self) -> WakeDecision | None:
        """Most recent verifier decision (for traces/tests)."""
        return self._last_decision
