"""Per-turn streaming STT coordinator.

The WebSocket layer owns transport; STT stream lifecycle, transcript throttling,
and STT-specific metrics live here with the rest of voice I/O.
"""

from __future__ import annotations

import logging
import time
import wave
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from core import settings
from core.id import generate_id
from core.voice.config import resolve_voice_config_sync
from core.voice.stt_service import STTBackend, StreamingSTTSession
from services.perf import perf

logger = logging.getLogger(__name__)

PartialTranscriptCallback = Callable[[str], Awaitable[None]]
ProviderTurnEndCallback = Callable[[str], Awaitable[None]]
BYTES_PER_SAMPLE = 2
INITIAL_AUDIO_CHUNK_SAMPLES = 1536


class StreamingSTTCoordinator:
    """Owns one active streaming STT session for a user turn."""

    def __init__(
        self,
        *,
        stt: STTBackend,
        session_id: str,
        turn_id: str | None = None,
        on_partial: PartialTranscriptCallback | None = None,
        on_provider_turn_end: ProviderTurnEndCallback | None = None,
    ) -> None:
        self._stt = stt
        self._session_id = session_id
        self._turn_id = turn_id
        self._stream_id = generate_id("stt-")
        self._on_partial = on_partial
        self._on_provider_turn_end = on_provider_turn_end
        self._stream: StreamingSTTSession | None = None
        self._last_partial = ""
        self._last_partial_at = 0.0
        self._first_partial_seen = False
        self._feed_count = 0
        self._bytes_fed = 0
        self._transcript_count = 0
        self._partial_emit_count = 0
        self._latest_text = ""
        self._latest_text_updated_at = 0.0
        self._latest_text_is_final = False
        self._debug_audio = bytearray() if settings.VOICE.stt_debug_dump_audio else None

    @property
    def active(self) -> bool:
        return self._stream is not None

    @property
    def latest_text(self) -> str:
        return self._latest_text

    @property
    def latest_text_updated_at(self) -> float:
        return self._latest_text_updated_at

    @property
    def latest_text_is_final(self) -> bool:
        return self._latest_text_is_final

    @property
    def stream_id(self) -> str:
        return self._stream_id

    @property
    def bytes_fed(self) -> int:
        return self._bytes_fed

    @property
    def feed_count(self) -> int:
        return self._feed_count

    async def start(self, initial_audio: bytes = b"") -> bool:
        if not settings.VOICE.stt_streaming_enabled or self._stream is not None:
            return False

        perf.start("stt_stream_start", self._session_id, turn_id=self._turn_id, stream_id=self._stream_id)
        try:
            stream = await self._stt.start_streaming(
                on_transcript=self._handle_transcript,
                on_turn_end=self._handle_provider_turn_end,
            )
        except Exception as exc:
            logger.warning("Streaming STT start failed; falling back to batch STT: %s", exc)
            perf.end(
                "stt_stream_start",
                self._session_id,
                turn_id=self._turn_id,
                stream_id=self._stream_id,
                status="error",
            )
            return False

        perf.end(
            "stt_stream_start",
            self._session_id,
            turn_id=self._turn_id,
            stream_id=self._stream_id,
            status="ok" if stream else "unavailable",
        )
        if stream is None:
            return False

        self._stream = stream
        perf.start("stt_stream_total", self._session_id, turn_id=self._turn_id, stream_id=self._stream_id)
        perf.start("stt_first_partial", self._session_id, turn_id=self._turn_id, stream_id=self._stream_id)

        if initial_audio:
            await self._feed_initial_audio(initial_audio)
        perf.log(
            "stt_stream_started",
            session=self._session_id,
            turn_id=self._turn_id,
            stream_id=self._stream_id,
            initial_audio_bytes=len(initial_audio),
            feed_count=self._feed_count,
            bytes_fed=self._bytes_fed,
        )
        return self._stream is not None

    async def _feed_initial_audio(self, audio_bytes: bytes) -> None:
        """Seed buffered turn audio using normal-size frames instead of one large write."""
        chunk_bytes = INITIAL_AUDIO_CHUNK_SAMPLES * settings.VOICE.channels * BYTES_PER_SAMPLE
        if len(audio_bytes) <= chunk_bytes:
            await self.feed(audio_bytes)
            return

        for offset in range(0, len(audio_bytes), chunk_bytes):
            if self._stream is None:
                return
            await self.feed(audio_bytes[offset: offset + chunk_bytes])

    async def feed(self, audio_bytes: bytes) -> None:
        if self._stream is None:
            return
        try:
            self._feed_count += 1
            self._bytes_fed += len(audio_bytes)
            if self._debug_audio is not None:
                self._debug_audio.extend(audio_bytes)
            await self._stream.feed(audio_bytes)
        except Exception as exc:
            logger.warning("Streaming STT feed failed; falling back to batch STT: %s", exc)
            await self.close(reason="feed_failed")

    async def finish(self) -> str | None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return None

        perf.start("stt_finalize_wait", self._session_id, turn_id=self._turn_id, stream_id=self._stream_id)
        try:
            finalize_timeout = (
                settings.VOICE.apple_speech_stream_finalize_timeout
                if resolve_voice_config_sync().stt_provider == "apple_speech"
                else settings.VOICE.stt_stream_finalize_timeout
            )
            transcript = await stream.finish(timeout_s=finalize_timeout)
            finalize_chars = len(transcript or "")
            latest_chars = len(self._latest_text)
            finalize_used_latest = bool(self._latest_text and latest_chars > finalize_chars)
            if finalize_used_latest:
                transcript = self._latest_text
            finish_status = getattr(stream, "last_finish_status", None)
            finish_stats = getattr(stream, "stats", {})
            perf.end(
                "stt_finalize_wait",
                self._session_id,
                turn_id=self._turn_id,
                stream_id=self._stream_id,
                status=finish_status or ("ok" if transcript else "empty"),
            )
            perf.log(
                "stt_stream_summary",
                session=self._session_id,
                turn_id=self._turn_id,
                stream_id=self._stream_id,
                status="finished",
                finish_status=finish_status,
                transcript_chars=len(transcript or ""),
                finalize_chars=finalize_chars,
                latest_text_chars=latest_chars,
                finalize_used_latest=finalize_used_latest,
                feed_count=self._feed_count,
                bytes_fed=self._bytes_fed,
                transcript_events=self._transcript_count,
                partials_emitted=self._partial_emit_count,
                provider_protocol=finish_stats.get("protocol") if isinstance(finish_stats, dict) else None,
                provider_events=finish_stats.get("events_seen") if isinstance(finish_stats, dict) else None,
                provider_interims=finish_stats.get("interim_seen") if isinstance(finish_stats, dict) else None,
                provider_finals=finish_stats.get("final_seen") if isinstance(finish_stats, dict) else None,
                provider_turn_ends=finish_stats.get("turn_end_seen") if isinstance(finish_stats, dict) else None,
            )
            # Single live stdout line per turn — reveals END/MIDDLE drops (provider side):
            # used_partial=True or finalize_status=timeout means Cartesia returned less
            # than the streamed partials.
            logger.info(
                "STT finalize | status=%s protocol=%s used_partial=%s chars=%d(final=%d) feeds=%d provider_finals=%s provider_turn_ends=%s text=%r",
                finish_status,
                finish_stats.get("protocol") if isinstance(finish_stats, dict) else None,
                finalize_used_latest,
                len(transcript or ""),
                finalize_chars,
                self._feed_count,
                finish_stats.get("final_seen") if isinstance(finish_stats, dict) else None,
                finish_stats.get("turn_end_seen") if isinstance(finish_stats, dict) else None,
                transcript or "",
            )
            self._dump_debug_audio(transcript or "")
            return transcript or None
        finally:
            perf.end("stt_stream_total", self._session_id, turn_id=self._turn_id, stream_id=self._stream_id)

    async def close(self, *, reason: str) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            await stream.close()
        finally:
            perf.log(
                "stt_stream_summary",
                session=self._session_id,
                turn_id=self._turn_id,
                stream_id=self._stream_id,
                status=f"closed:{reason}",
                latest_text_chars=len(self._latest_text),
                feed_count=self._feed_count,
                bytes_fed=self._bytes_fed,
                transcript_events=self._transcript_count,
                partials_emitted=self._partial_emit_count,
            )
            perf.end(
                "stt_stream_total",
                self._session_id,
                turn_id=self._turn_id,
                stream_id=self._stream_id,
                status=f"closed:{reason}",
            )
            self._dump_debug_audio(self._latest_text)

    def _dump_debug_audio(self, transcript: str) -> None:
        if self._debug_audio is None or not self._debug_audio:
            return
        try:
            dump_dir = Path(settings.VOICE.stt_debug_dump_dir)
            if not dump_dir.is_absolute():
                dump_dir = Path.cwd() / dump_dir
            dump_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            turn_id = self._turn_id or "turn"
            path = dump_dir / f"{timestamp}_{turn_id}_{self._stream_id}.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(settings.VOICE.channels)
                wav.setsampwidth(BYTES_PER_SAMPLE)
                wav.setframerate(settings.VOICE.sample_rate)
                wav.writeframes(bytes(self._debug_audio))

            duration_ms = (
                self._bytes_fed
                / (settings.VOICE.sample_rate * settings.VOICE.channels * BYTES_PER_SAMPLE)
                * 1000
            )
            logger.info(
                "Saved STT debug audio | path=%s duration_ms=%.1f transcript=%r",
                path,
                duration_ms,
                transcript,
            )
        except Exception as exc:
            logger.warning("Failed to save STT debug audio: %s", exc)
        finally:
            self._debug_audio = None

    async def _handle_transcript(self, text: str, is_final: bool) -> None:
        cleaned = text.strip()
        if not cleaned:
            return

        previous_chars = len(self._latest_text)
        incoming_chars = len(cleaned)
        self._transcript_count += 1
        transcript_regressed = incoming_chars < previous_chars
        if settings.VOICE.trace_voice_events or transcript_regressed:
            perf.log(
                "stt_stream_transcript_received",
                session=self._session_id,
                turn_id=self._turn_id,
                stream_id=self._stream_id,
                transcript_index=self._transcript_count,
                text_chars=incoming_chars,
                previous_chars=previous_chars,
                delta_chars=incoming_chars - previous_chars,
                regressed=transcript_regressed,
                is_final=is_final,
                feed_count=self._feed_count,
                bytes_fed=self._bytes_fed,
            )
        if cleaned != self._latest_text:
            self._latest_text_updated_at = time.monotonic()
        self._latest_text = cleaned
        self._latest_text_is_final = is_final

        if not self._first_partial_seen:
            self._first_partial_seen = True
            perf.end(
                "stt_first_partial",
                self._session_id,
                turn_id=self._turn_id,
                stream_id=self._stream_id,
                is_final=is_final,
            )
            perf.log(
                "stt_first_partial",
                session=self._session_id,
                turn_id=self._turn_id,
                stream_id=self._stream_id,
                text_chars=len(cleaned),
                is_final=is_final,
                feed_count=self._feed_count,
                bytes_fed=self._bytes_fed,
            )

        if self._on_partial is None:
            return

        now = time.monotonic()
        min_interval = settings.VOICE.stt_partial_emit_interval_s
        if not is_final and cleaned == self._last_partial:
            return
        if not is_final and (now - self._last_partial_at) < min_interval:
            return

        self._last_partial = cleaned
        self._last_partial_at = now
        try:
            await self._on_partial(cleaned)
            self._partial_emit_count += 1
            if settings.VOICE.trace_voice_events:
                perf.log(
                    "stt_partial_emitted",
                    session=self._session_id,
                    turn_id=self._turn_id,
                    stream_id=self._stream_id,
                    text_chars=len(cleaned),
                    is_final=is_final,
                    partial_index=self._partial_emit_count,
                )
        except Exception as exc:
            logger.debug("Partial transcript callback failed: %s", exc)

    async def _handle_provider_turn_end(self, text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            if cleaned != self._latest_text:
                self._latest_text_updated_at = time.monotonic()
            self._latest_text = cleaned
        self._latest_text_is_final = True
        perf.log(
            "stt_provider_turn_end",
            session=self._session_id,
            turn_id=self._turn_id,
            stream_id=self._stream_id,
            text_chars=len(self._latest_text),
            feed_count=self._feed_count,
            bytes_fed=self._bytes_fed,
        )
        if self._on_provider_turn_end is not None:
            await self._on_provider_turn_end(self._latest_text)
