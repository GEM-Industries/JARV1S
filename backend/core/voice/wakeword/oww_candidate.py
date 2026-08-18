from __future__ import annotations

import logging
from collections import deque
from pathlib import Path

import numpy as np
from openwakeword.model import Model

from core.voice.wakeword.types import WakeCandidate

logger = logging.getLogger(__name__)

# Ring buffer stores recent PCM windows. maxlen=19 gives ~1.5s of sliding history for
# detection, but we capture only the last 16 windows on detection — exactly the frame
# context OWW evaluated — which is also what _extract_embeddings produces one training
# window from (stride=4 with 16 frames → range(0,1,4) = [0]).
_RING_BUFFER_WINDOWS = 19
_FEEDBACK_CAPTURE_WINDOWS = 16  # must match OWW's feature window size

INFERENCE_WINDOW_SAMPLES = 1280
BYTES_PER_SAMPLE = 2
INFERENCE_WINDOW_BYTES = INFERENCE_WINDOW_SAMPLES * BYTES_PER_SAMPLE


class OWWCandidateStage:
    """openWakeWord Stage-1 candidate detector with sustain filter."""

    def __init__(
        self,
        model_path: Path,
        *,
        sensitivity: float,
        consecutive_required: int,
        vad_threshold: float,
    ):
        self.sensitivity = sensitivity
        self.consecutive_required = max(1, consecutive_required)
        self.vad_threshold = vad_threshold
        self._model_path = model_path
        self._model_loaded = False
        self.inference_count = 0
        self._consecutive = 0
        self._model_name: str | None = None
        self.buffer = bytearray()
        self._ring_buffer: deque[bytes] = deque(maxlen=_RING_BUFFER_WINDOWS)
        self.model: Model | None = None
        self._load_model()

    def _load_model(self) -> None:
        if not self._model_path.exists():
            logger.error("Wake Word model not found at %s", self._model_path)
            return
        try:
            try:
                self.model = self._build_model(self.vad_threshold)
            except Exception as vad_err:
                if self.vad_threshold > 0:
                    logger.warning("VAD unavailable (%s); loading wake word without VAD.", vad_err)
                    self.vad_threshold = 0.0
                    self.model = self._build_model(0.0)
                else:
                    raise
            self._model_name = next(iter(self.model.models.keys()), None)
            self._model_loaded = True
        except Exception as exc:
            logger.error("Failed to initialize OWW candidate stage: %s", exc)
            self._model_loaded = False

    def _build_model(self, vad_threshold: float) -> Model:
        return Model(
            wakeword_models=[str(self._model_path)],
            inference_framework="onnx",
            vad_threshold=vad_threshold,
        )

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded

    def reload(self) -> None:
        self._model_loaded = False
        self.buffer.clear()
        self._ring_buffer.clear()
        self._consecutive = 0
        self._load_model()

    def reset(self) -> None:
        if self._model_loaded and self.model is not None:
            self.model.reset()
        self.buffer.clear()
        self._consecutive = 0

    def process(self, chunk: bytes) -> WakeCandidate | None:
        if not self._model_loaded or self.model is None or self._model_name is None:
            return None

        self.buffer.extend(chunk)

        while len(self.buffer) >= INFERENCE_WINDOW_BYTES:
            window_bytes = bytes(self.buffer[:INFERENCE_WINDOW_BYTES])
            del self.buffer[:INFERENCE_WINDOW_BYTES]
            self._ring_buffer.append(window_bytes)

            audio_data = np.frombuffer(window_bytes, dtype=np.int16)
            self.inference_count += 1
            prediction = self.model.predict(audio_data)
            score = prediction.get(self._model_name, 0.0)

            if score >= self.sensitivity:
                self._consecutive += 1
            else:
                self._consecutive = 0

            if self._consecutive >= self.consecutive_required:
                logger.info(
                    "Wake candidate (%s: %.4f, %d frames)",
                    self._model_name,
                    score,
                    self._consecutive,
                )
                tail = bytes(self.buffer[:INFERENCE_WINDOW_BYTES // 2])
                audio = b"".join(list(self._ring_buffer)[-_FEEDBACK_CAPTURE_WINDOWS:]) + tail
                candidate = WakeCandidate(audio=audio, score=score, stage="oww")
                self._consecutive = 0
                self.buffer.clear()
                return candidate

        return None
