"""Audio end-of-turn detection for voice turns.

VAD proposes endpoint candidates; this module decides from recent PCM whether
the user has finished speaking, using LiveKit's local v1-mini TurnDetector.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from livekit import rtc

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from livekit.agents.inference.eot.detector import TurnDetector as LiveKitTurnDetector


@dataclass(frozen=True)
class TurnDecision:
    done: bool
    confidence: float | None = None
    reason: str = "unknown"


_detector: LiveKitTurnDetector | None = None
_MAX_FRAME_DURATION_SECONDS = 0.1


def _get_detector() -> Any:
    global _detector
    if _detector is None:
        from livekit.agents.inference import TurnDetector as LiveKitTurnDetector

        _detector = LiveKitTurnDetector(version="v1-mini")
    return _detector


class AudioTurnDetectorSession:
    """Per-connection streaming audio EOU detector."""

    def __init__(self, *, sample_rate: int = 16000, channels: int = 1) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._stream: Any | None = None
        self._failed = False

    def _ensure_stream(self) -> Any | None:
        if self._failed:
            return None
        if self._stream is None:
            try:
                self._stream = _get_detector().stream()
            except Exception as exc:
                logger.warning("Audio turn detector unavailable; falling back to VAD: %s", exc)
                self._failed = True
                return None
        return self._stream

    def push_pcm(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        stream = self._ensure_stream()
        if stream is None:
            return
        bytes_per_sample = 2 * self._channels
        usable_bytes = len(pcm_bytes) - (len(pcm_bytes) % bytes_per_sample)
        if usable_bytes <= 0:
            return
        try:
            max_frame_bytes = (
                int(self._sample_rate * _MAX_FRAME_DURATION_SECONDS) * bytes_per_sample
            )
            for offset in range(0, usable_bytes, max_frame_bytes):
                data = pcm_bytes[offset : min(offset + max_frame_bytes, usable_bytes)]
                stream.push_audio(
                    rtc.AudioFrame(
                        data=data,
                        sample_rate=self._sample_rate,
                        num_channels=self._channels,
                        samples_per_channel=len(data) // bytes_per_sample,
                    )
                )
        except Exception:
            logger.debug("Audio turn detector push failed for one frame", exc_info=True)

    async def predict(self, *, language: str = "en") -> TurnDecision:
        stream = self._ensure_stream()
        if stream is None:
            return TurnDecision(done=True, reason="vad_fallback")
        try:
            from livekit.agents.language import LanguageCode

            prediction = asyncio.ensure_future(stream.predict())
            try:
                event = await asyncio.wait_for(
                    asyncio.shield(prediction),
                    timeout=stream.prediction_timeout,
                )
            except TimeoutError:
                stream.cancel_inference(timed_out=True)
                logger.warning("Audio turn detector prediction timed out; VAD commit this turn")
                return TurnDecision(done=True, reason="vad_fallback")
            threshold = await stream.unlikely_threshold(LanguageCode(language))
            probability = float(event.end_of_turn_probability)
            if threshold is None:
                return TurnDecision(done=True, confidence=probability, reason="no_threshold")
            return TurnDecision(
                done=probability >= threshold,
                confidence=probability,
                reason="audio_eou",
            )
        except Exception as exc:
            logger.warning("Audio turn detector prediction failed; VAD commit this turn: %s", exc)
            return TurnDecision(done=True, reason="vad_fallback")

    def flush(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.flush()
        except Exception:
            logger.debug("Audio turn detector flush failed", exc_info=True)

    async def aclose(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            await stream.aclose()
        except Exception:
            logger.debug("Audio turn detector close failed", exc_info=True)
