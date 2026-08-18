"""Background diagnostics sampler for user-facing system health metrics.

Runs a 5-second asyncio loop that caches CPU%, memory, voice pipeline state,
last-turn timings, event loop lag, and thread count. The pong handler reads
the cached dict at zero cost.
"""

import asyncio
import logging
import os
import threading
import time
from collections import Counter
from typing import Any, Dict, Optional

import psutil

logger = logging.getLogger(__name__)

_LOOP_LAG_SPIKE_THRESHOLD_MS = 20.0
_THREAD_SPIKE_THRESHOLD = 35


class DiagnosticsService:
    def __init__(self) -> None:
        self._process = psutil.Process(os.getpid())
        self._process.cpu_percent()  # prime — first call always returns 0.0
        self._start = time.monotonic()
        self._cached: Dict[str, Any] = {}
        self._task: Optional["asyncio.Task[None]"] = None
        self._last_turn_model: Optional[str] = None
        self._last_thread_warning_count = 0

    def record_turn_model(self, model: str) -> None:
        """Stamp the model used for the most recent turn (called by orchestrator)."""
        if model:
            self._last_turn_model = model

    async def start(self) -> None:
        self._task = asyncio.create_task(self._sample_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def snapshot(self) -> Dict[str, Any]:
        """Pre-computed diagnostics dict — zero-cost property read for pong handler."""
        return self._cached

    async def _measure_loop_lag(self) -> float:
        """Measure event loop responsiveness in ms. High values indicate a blocking call."""
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        await asyncio.sleep(0)
        return (loop.time() - t0) * 1000

    async def _sample_loop(self) -> None:
        while True:
            try:
                loop_lag_ms = round(await self._measure_loop_lag(), 2)
                thread_count = threading.active_count()

                with self._process.oneshot():
                    cpu = self._process.cpu_percent()
                    mem_mb = round(self._process.memory_info().rss / 1_048_576)

                self._cached = {
                    "cpu_percent": round(cpu, 1),
                    "memory_mb": mem_mb,
                    "loop_lag_ms": loop_lag_ms,
                    "thread_count": thread_count,
                    "uptime_s": int(time.monotonic() - self._start),
                    "voice": self._voice_snapshot(),
                    "turn": self._turn_snapshot(),
                }

                if loop_lag_ms > _LOOP_LAG_SPIKE_THRESHOLD_MS:
                    logger.warning("Loop lag spike: lag_ms=%s threads=%s", loop_lag_ms, thread_count)
                if self._should_warn_thread_count(thread_count):
                    logger.warning(
                        "Thread count spike: threads=%s breakdown=%s",
                        thread_count,
                        self._thread_breakdown(),
                    )

            except Exception:
                logger.debug("Diagnostics sample failed", exc_info=True)
            await asyncio.sleep(5)

    def _voice_snapshot(self) -> Dict[str, Any]:
        try:
            from api.websockets.connection import manager
            from core.config import settings
            from core.voice.processor import VoiceMode
            import glob as glob_module

            session = manager.get_session(settings.DEFAULT_USER_ID)
            if not session:
                return {"mode": "disconnected", "wakeword_inferences_sec": 0, "wakeword_feedback": None}

            ww = session.processor.wakeword_service
            inferences_sec = 0
            if ww:
                inferences_sec = round(ww.inference_count / 5)
                ww.inference_count = 0

            feedback_base = settings.BASE_DIR.parent / "training" / "wakeword" / "data" / "feedback"
            pos_count = len(glob_module.glob(str(feedback_base / "positives" / "*.wav")))
            neg_count = len(glob_module.glob(str(feedback_base / "negatives" / "*.wav")))

            return {
                "mode": session.processor.mode.name.lower(),
                "wakeword_inferences_sec": inferences_sec,
                "wakeword_save_positive_feedback": settings.VOICE.wakeword_save_positive_feedback,
                "wakeword_feedback": {"positive_count": pos_count, "negative_count": neg_count},
            }
        except Exception:
            return {"mode": "unknown", "wakeword_inferences_sec": 0, "wakeword_feedback": None}

    def _turn_snapshot(self) -> Dict[str, Any]:
        try:
            from services.perf import perf

            summary = perf.latest_turn_summary()
            if summary is not None:
                model = summary.get("model") or self._last_turn_model
                summary["model"] = model
                return summary

            return {
                "turn_id": None,
                "source": None,
                "modality": None,
                "delivery": None,
                "origin": None,
                "status": None,
                "response_ms": None,
                "total_ms": None,
                "stages": [],
                "model": self._last_turn_model,
            }
        except Exception:
            return {
                "turn_id": None,
                "source": None,
                "modality": None,
                "delivery": None,
                "origin": None,
                "status": None,
                "response_ms": None,
                "total_ms": None,
                "stages": [],
                "model": self._last_turn_model,
            }

    def _should_warn_thread_count(self, thread_count: int) -> bool:
        if thread_count <= _THREAD_SPIKE_THRESHOLD:
            self._last_thread_warning_count = 0
            return False

        # Warn when first crossing the threshold or when the process creates more
        # Python threads. Avoid repeating the same count every sample forever.
        if self._last_thread_warning_count == 0 or thread_count > self._last_thread_warning_count:
            self._last_thread_warning_count = thread_count
            return True
        return False

    @staticmethod
    def _thread_breakdown() -> Dict[str, int]:
        counts: Counter[str] = Counter()
        for thread in threading.enumerate():
            name = thread.name
            if name.startswith("jarvis-asyncio"):
                key = "jarvis-asyncio"
            elif name.startswith("asyncio"):
                key = "asyncio-default"
            elif name.startswith("ThreadPoolExecutor"):
                key = "thread-pool"
            elif name.startswith("Thread-"):
                key = "thread"
            else:
                key = name
            counts[key] += 1
        return dict(counts.most_common())


diagnostics_service = DiagnosticsService()
