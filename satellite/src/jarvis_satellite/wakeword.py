"""Optional on-device PASSIVE wake detector for edge-wake satellites."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

INFERENCE_WINDOW_SAMPLES = 1280
BYTES_PER_SAMPLE = 2
INFERENCE_WINDOW_BYTES = INFERENCE_WINDOW_SAMPLES * BYTES_PER_SAMPLE
@dataclass(frozen=True, slots=True)
class LocalWakeHit:
    score: float


class WakeDetector(Protocol):
    def process(self, chunk: bytes) -> LocalWakeHit | None: ...

    def reset(self) -> None: ...

    @property
    def model_loaded(self) -> bool: ...


class OpenWakeWordDetector:
    """Minimal openWakeWord Stage-1 detector (same windowing as the Host)."""

    def __init__(
        self,
        model_path: Path,
        *,
        sensitivity: float = 0.70,
        consecutive_required: int = 3,
        vad_threshold: float = 0.5,
    ) -> None:
        self.sensitivity = sensitivity
        self.consecutive_required = max(1, consecutive_required)
        self.vad_threshold = vad_threshold
        self._model_path = model_path
        self._model_loaded = False
        self._consecutive = 0
        self._model_name: str | None = None
        self.buffer = bytearray()
        self.model = None
        self._load_model()

    def _load_model(self) -> None:
        if not self._model_path.exists():
            logger.error("Wake word model not found at %s", self._model_path)
            return
        try:
            import numpy as np
            from openwakeword.model import Model
        except ImportError as exc:
            logger.error(
                "edge_wakeword requires optional deps (openwakeword/onnxruntime/numpy): %s",
                exc,
            )
            return

        try:
            try:
                self.model = Model(
                    wakeword_models=[str(self._model_path)],
                    inference_framework="onnx",
                    vad_threshold=self.vad_threshold,
                )
            except Exception as vad_err:
                if self.vad_threshold > 0:
                    logger.warning("VAD unavailable (%s); loading wake word without VAD.", vad_err)
                    self.vad_threshold = 0.0
                    self.model = Model(
                        wakeword_models=[str(self._model_path)],
                        inference_framework="onnx",
                        vad_threshold=0.0,
                    )
                else:
                    raise
            self._model_name = next(iter(self.model.models.keys()), None)
            if self._model_name is None:
                raise RuntimeError("wakeword model did not expose an output")
            self._np = np
            self._model_loaded = True
            logger.info("Loaded edge wakeword model from %s", self._model_path)
        except Exception as exc:
            logger.error("Failed to initialize edge wakeword: %s", exc)
            self._model_loaded = False

    @property
    def model_loaded(self) -> bool:
        return self._model_loaded

    def reset(self) -> None:
        if self._model_loaded and self.model is not None:
            self.model.reset()
        self.buffer.clear()
        self._consecutive = 0

    def process(self, chunk: bytes) -> LocalWakeHit | None:
        if not self._model_loaded or self.model is None or self._model_name is None:
            return None

        self.buffer.extend(chunk)
        while len(self.buffer) >= INFERENCE_WINDOW_BYTES:
            window_bytes = bytes(self.buffer[:INFERENCE_WINDOW_BYTES])
            del self.buffer[:INFERENCE_WINDOW_BYTES]

            audio_data = self._np.frombuffer(window_bytes, dtype=self._np.int16)
            prediction = self.model.predict(audio_data)
            score = float(prediction.get(self._model_name, 0.0))

            if score >= self.sensitivity:
                self._consecutive += 1
            else:
                self._consecutive = 0

            if self._consecutive >= self.consecutive_required:
                logger.info("Edge wake candidate score=%.4f frames=%d", score, self._consecutive)
                self._consecutive = 0
                self.buffer.clear()
                return LocalWakeHit(score=score)
        return None


def build_wake_detector(
    model_path: Path,
    *,
    sensitivity: float,
    consecutive_required: int,
    vad_threshold: float,
) -> WakeDetector | None:
    detector = OpenWakeWordDetector(
        model_path,
        sensitivity=sensitivity,
        consecutive_required=consecutive_required,
        vad_threshold=vad_threshold,
    )
    if not detector.model_loaded:
        return None
    return detector
