"""Speech-to-Text backends: protocol definition + Cartesia, Apple Speech, and eval-only MLX."""

import logging
import asyncio
import contextlib
import importlib
import json
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Optional, Protocol

from urllib.parse import urlencode

from core import settings
from core.credentials.store import credential_store
from core.voice.helper_client import request_helper

logger = logging.getLogger(__name__)

TranscriptCallback = Callable[[str, bool], Awaitable[None]]
ProviderTurnEndCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class STTCapabilities:
    streaming_partials: bool = False
    provider_turn_events: bool = False


@dataclass(frozen=True)
class AppleSpeechEvent:
    text: str = ""
    is_final: bool = False
    is_turn_end: bool = False
    is_terminal: bool = False


class StreamingSTTSession(Protocol):
    async def feed(self, audio_bytes: bytes) -> None: ...
    async def finish(self, *, timeout_s: float) -> str: ...
    async def close(self) -> None: ...


class STTBackend(Protocol):
    capabilities: STTCapabilities

    async def initialize(self) -> None: ...
    async def transcribe_batched(self, audio_bytes: bytes) -> str: ...
    async def start_streaming(
        self,
        on_transcript: TranscriptCallback | None = None,
        on_turn_end: ProviderTurnEndCallback | None = None,
    ) -> Optional[StreamingSTTSession]: ...


class MLXSTTService:
    """Batched STT via MLX-Whisper on Apple Silicon (Metal GPU)."""

    SAMPLE_RATE = 16000
    capabilities = STTCapabilities()

    # Metal can't run two transcriptions at once — concurrent command buffers
    # abort the process ("IOGPUMetalCommandBuffer validate: uncommitted encoder").
    # asyncio.to_thread cancellation (e.g. fast recovery) leaves the worker thread
    # running, so the lock must live in the thread to truly serialize GPU access.
    _gpu_lock = threading.Lock()

    def __init__(self, model_size: str = "mlx-community/whisper-tiny.en-mlx-4bit", language: str = "en"):
        self.model_size = model_size
        self.language = language

    def _transcribe_sync(self, audio_np) -> str:
        import mlx_whisper
        with self._gpu_lock:
            result = mlx_whisper.transcribe(
                audio_np,
                path_or_hf_repo=self.model_size,
                language=self.language,
            )
        return (result.get("text") or "").strip()

    async def initialize(self) -> None:
        import numpy as np
        logger.info(f"Warming up MLX-Whisper: {self.model_size}")
        warmup = np.zeros(int(self.SAMPLE_RATE * 0.1), dtype=np.float32)
        await asyncio.to_thread(self._transcribe_sync, warmup)
        logger.info("MLX-Whisper ready")

    async def transcribe_batched(self, audio_bytes: bytes) -> str:
        if not audio_bytes:
            return ""
        import numpy as np
        try:
            audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            logger.debug(f"Transcribing {len(audio_np) / self.SAMPLE_RATE:.2f}s of audio...")
            text = await asyncio.to_thread(self._transcribe_sync, audio_np)
            # Discard common silence hallucinations
            if text.lower() in {"i have no regrets.", "thanks for watching.", "subtitles by"}:
                return ""
            if text:
                logger.info(f"STT: {text}")
            return text
        except Exception as e:
            logger.error(f"MLX-Whisper transcription error: {e}")
            return ""

    async def start_streaming(
        self,
        on_transcript: TranscriptCallback | None = None,
        on_turn_end: ProviderTurnEndCallback | None = None,
    ) -> Optional[StreamingSTTSession]:
        return None


class CartesiaSTTService:
    """Streaming STT via Cartesia using the product-selected short-command model."""

    SAMPLE_RATE = 16000
    MODEL = "ink-whisper"

    def __init__(self, language: str = "en"):
        self.language = language
        self._client = None
        self.capabilities = STTCapabilities(
            streaming_partials=True,
            provider_turn_events=False,
        )

    async def initialize(self) -> None:
        from cartesia import AsyncCartesia  # type: ignore[import-not-found]
        api_key = credential_store.get_stored_secret("CARTESIA_API_KEY")
        if not api_key:
            logger.warning("Cartesia STT unavailable: CARTESIA_API_KEY is not stored")
            return
        self._client = AsyncCartesia(api_key=api_key)
        logger.info("Cartesia STT ready")

    async def transcribe_batched(self, audio_bytes: bytes) -> str:
        if not audio_bytes or self._client is None:
            return ""
        try:
            # Pipeline produces 16-bit mono PCM at 16kHz — send directly, no conversion.
            result = await self._client.stt.transcribe(
                file=audio_bytes,
                model=self.MODEL,
                encoding="pcm_s16le",
                sample_rate=self.SAMPLE_RATE,
                language=self.language,
            )
            text = (result.text or "").strip()
            if text:
                logger.info(f"STT: {text}")
            return text
        except Exception as e:
            logger.error(f"Cartesia STT error: {e}")
            return ""

    async def start_streaming(
        self,
        on_transcript: TranscriptCallback | None = None,
        on_turn_end: ProviderTurnEndCallback | None = None,
    ) -> Optional[StreamingSTTSession]:
        api_key = credential_store.get_stored_secret("CARTESIA_API_KEY")
        if not api_key:
            logger.warning("Cartesia streaming STT unavailable: CARTESIA_API_KEY is not stored")
            return None
        session: StreamingSTTSession = CartesiaStreamingSTTSession(
            api_key=api_key,
            model=self.MODEL,
            language=self.language,
            sample_rate=self.SAMPLE_RATE,
            on_transcript=on_transcript,
        )
        await session.start()
        return session


class CartesiaStreamingSTTSession:
    """Low-level Cartesia STT websocket session for one user utterance."""

    API_VERSION = "2025-04-16"

    def __init__(
        self,
        *,
        api_key: str,
        language: str,
        sample_rate: int,
        model: str = "ink-whisper",
        on_transcript: TranscriptCallback | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.language = language
        self.sample_rate = sample_rate
        self._on_transcript = on_transcript
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None
        self._final_after_finalize_event = asyncio.Event()
        self._done_event = asyncio.Event()
        self._latest_interim = ""
        self._final_parts: list[str] = []
        self._finalizing = False
        self._closed = False
        self._chunks_sent = 0
        self._bytes_sent = 0
        self._events_seen = 0
        self._interim_seen = 0
        self._final_seen = 0
        self.last_finish_status: str | None = None

    @property
    def stats(self) -> dict[str, int | str]:
        return {
            "protocol": "cartesia",
            "events_seen": self._events_seen,
            "interim_seen": self._interim_seen,
            "final_seen": self._final_seen,
            "chunks_sent": self._chunks_sent,
            "bytes_sent": self._bytes_sent,
        }

    async def start(self) -> None:
        websockets = importlib.import_module("websockets")

        params = urlencode(
            {
                "model": self.model,
                "sample_rate": str(self.sample_rate),
                "encoding": "pcm_s16le",
                "cartesia_version": self.API_VERSION,
                "api_key": self.api_key,
                "language": self.language,
            }
        )
        self._ws = await websockets.connect(f"wss://api.cartesia.ai/stt/websocket?{params}")
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def feed(self, audio_bytes: bytes) -> None:
        if not audio_bytes or self._ws is None or self._closed:
            return
        self._chunks_sent += 1
        self._bytes_sent += len(audio_bytes)
        await self._ws.send(audio_bytes)

    async def finish(self, *, timeout_s: float) -> str:
        if self._ws is None:
            return ""
        try:
            self._finalizing = True
            await self._ws.send("finalize")
            await asyncio.wait_for(self._wait_for_terminal_transcript(), timeout=timeout_s)
            text = self._assembled_text(include_interim=True)
            self.last_finish_status = "ok" if text.strip() else "empty"
            return text
        except asyncio.TimeoutError:
            text = self._assembled_text(include_interim=True)
            self.last_finish_status = "timeout"
            logger.warning(
                "Cartesia streaming STT finalize timed out; partial_chars=%d final_segments=%d bytes_sent=%d",
                len(text),
                len(self._final_parts),
                self._bytes_sent,
            )
            return text
        except Exception as e:
            self.last_finish_status = "error"
            logger.error("Cartesia streaming STT finalize error: %s", e)
            return ""
        finally:
            self._schedule_background_close()

    def _schedule_background_close(self) -> None:
        if self._closed:
            return
        try:
            asyncio.get_running_loop().create_task(self._close_background())
        except RuntimeError:
            pass

    async def _close_background(self) -> None:
        try:
            await self.close()
        except Exception as exc:
            logger.debug("Cartesia streaming STT background close failed: %s", exc)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            await self._ws.close()
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                message_type = data.get("type")
                self._events_seen += 1
                if message_type == "transcript":
                    text = data.get("text") or ""
                    if data.get("is_final"):
                        self._final_seen += 1
                        self._latest_interim = ""
                        self._append_final(text)
                        await self._emit_transcript(is_final=self._finalizing)
                        if self._finalizing:
                            self._final_after_finalize_event.set()
                    elif text.strip():
                        self._interim_seen += 1
                        self._latest_interim = text
                        await self._emit_transcript(is_final=False)
                elif message_type in {"done", "flush_done"}:
                    self._done_event.set()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._closed:
                logger.error(
                    "Cartesia streaming STT receive error: %s | chunks=%d bytes=%d events=%d interim=%d final=%d",
                    e,
                    self._chunks_sent,
                    self._bytes_sent,
                    self._events_seen,
                    self._interim_seen,
                    self._final_seen,
                )
                self._done_event.set()

    async def _wait_for_terminal_transcript(self) -> None:
        # Cartesia can emit multiple final transcript segments after "finalize".
        # Treat the provider's flush/done event as terminal; otherwise an early
        # final segment can make us return a shorter transcript than the partial
        # callback just emitted.
        await self._done_event.wait()

    def _append_final(self, text: str) -> None:
        if not text.strip():
            return
        self._final_parts.append(text)

    def _assembled_text(self, *, include_interim: bool) -> str:
        parts = list(self._final_parts)
        if include_interim and self._latest_interim:
            parts.append(self._latest_interim)
        return "".join(parts)

    async def _emit_transcript(self, *, is_final: bool) -> None:
        if self._on_transcript is None:
            return
        text = self._assembled_text(include_interim=not is_final)
        if text.strip():
            await self._on_transcript(text, is_final)


def _parse_apple_stt_event(payload: dict) -> AppleSpeechEvent:
    """Normalize Apple Speech helper JSON into JARV1S' transcript contract.

    Only ``done`` is terminal. ``final`` / ``partial`` are cumulative text snapshots.
    """
    event_type = str(payload.get("type") or payload.get("event") or "").lower()
    text = (
        payload.get("text")
        or payload.get("transcript")
        or payload.get("partial")
        or ""
    )

    is_terminal = event_type == "done"
    is_final = event_type == "final" or bool(payload.get("is_final") or payload.get("final"))
    if event_type == "partial":
        is_final = False
    return AppleSpeechEvent(
        text=str(text).strip(),
        is_final=is_final,
        is_turn_end=False,
        is_terminal=is_terminal,
    )

def _apple_speech_control_fields() -> dict[str, str]:
    token = (settings.VOICE.apple_speech_token or "").strip()
    return {"token": token} if token else {}


class AppleSpeechHelperClient:
    """Control-plane client for Apple Speech readiness and preparation."""

    def __init__(self, *, url: str | None = None) -> None:
        self.url = url or settings.VOICE.apple_speech_url

    async def status(self) -> dict:
        return await self._request(
            "status",
            reply_timeout_s=settings.VOICE.apple_speech_connect_timeout,
        )

    async def prepare(self) -> dict:
        # On-device speech models can take several minutes to download.
        return await self._request("prepare", reply_timeout_s=600.0)

    async def _request(self, message_type: str, *, reply_timeout_s: float) -> dict:
        return await request_helper(
            self.url,
            message_type,
            fields=_apple_speech_control_fields(),
            connect_timeout_s=settings.VOICE.apple_speech_connect_timeout,
            reply_timeout_s=reply_timeout_s,
        )


class AppleSpeechSTTService:
    """Adapter for the supervised Apple SpeechAnalyzer helper."""

    SAMPLE_RATE = 16000

    def __init__(self, *, url: str | None = None) -> None:
        self.url = url or settings.VOICE.apple_speech_url
        # Host owns endpointing; helper only streams cumulative partials/finals.
        self.capabilities = STTCapabilities(
            streaming_partials=True,
            provider_turn_events=False,
        )

    async def initialize(self) -> None:
        logger.info("Apple Speech STT configured: %s", self.url)

    async def transcribe_batched(self, audio_bytes: bytes) -> str:
        logger.warning("Apple Speech STT does not support batch transcription.")
        return ""

    async def start_streaming(
        self,
        on_transcript: TranscriptCallback | None = None,
        on_turn_end: ProviderTurnEndCallback | None = None,
    ) -> Optional[StreamingSTTSession]:
        del on_turn_end  # Apple path never uses provider turn events.
        session = AppleSpeechSTTSession(
            url=self.url,
            sample_rate=self.SAMPLE_RATE,
            on_transcript=on_transcript,
        )
        await session.start()
        return session

class AppleSpeechSTTSession:
    """WebSocket session for the Apple Speech helper (one utterance)."""

    def __init__(
        self,
        *,
        url: str,
        sample_rate: int,
        on_transcript: TranscriptCallback | None = None,
    ) -> None:
        self.url = url
        self.sample_rate = sample_rate
        self._on_transcript = on_transcript
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None
        self._started_event = asyncio.Event()
        self._done_event = asyncio.Event()
        self._start_error: str | None = None
        self._closed = False
        self._latest_text = ""
        self._chunks_sent = 0
        self._bytes_sent = 0
        self._events_seen = 0
        self._interim_seen = 0
        self._final_seen = 0
        self.last_finish_status: str | None = None
        self.last_status: dict | None = None

    @property
    def stats(self) -> dict[str, int | str]:
        return {
            "protocol": "apple_speech",
            "events_seen": self._events_seen,
            "interim_seen": self._interim_seen,
            "final_seen": self._final_seen,
            "chunks_sent": self._chunks_sent,
            "bytes_sent": self._bytes_sent,
        }

    async def start(self) -> None:
        websockets = importlib.import_module("websockets")
        self._ws = await asyncio.wait_for(
            websockets.connect(self.url),
            timeout=settings.VOICE.apple_speech_connect_timeout,
        )
        self._recv_task = asyncio.create_task(self._recv_loop())
        await self._send_start_message()
        try:
            await asyncio.wait_for(
                self._started_event.wait(),
                timeout=settings.VOICE.apple_speech_connect_timeout,
            )
        except Exception:
            await self.close()
            raise
        if self._start_error:
            await self.close()
            raise RuntimeError(self._start_error)

    async def _send_start_message(self) -> None:
        if self._ws is None:
            return
        message = {
            "type": "start",
            "encoding": "pcm_s16le",
            "sample_rate": self.sample_rate,
            "channels": 1,
            **_apple_speech_control_fields(),
        }
        with contextlib.suppress(Exception):
            await self._ws.send(json.dumps(message))

    async def feed(self, audio_bytes: bytes) -> None:
        if not audio_bytes or self._ws is None or self._closed:
            return
        self._chunks_sent += 1
        self._bytes_sent += len(audio_bytes)
        await self._ws.send(audio_bytes)

    async def finish(self, *, timeout_s: float) -> str:
        if self._ws is None:
            return self._latest_text.strip()
        try:
            await self._ws.send(json.dumps({"type": "finalize", **_apple_speech_control_fields()}))
            await asyncio.wait_for(self._done_event.wait(), timeout=timeout_s)
            self.last_finish_status = "ok" if self._latest_text.strip() else "empty"
            return self._latest_text.strip()
        except asyncio.TimeoutError:
            self.last_finish_status = "timeout"
            return self._latest_text.strip()
        except Exception as exc:
            self.last_finish_status = "error"
            logger.error("Apple Speech STT finalize error: %s", exc)
            return self._latest_text.strip()
        finally:
            self._schedule_background_close()

    def _schedule_background_close(self) -> None:
        if self._closed:
            return
        try:
            asyncio.get_running_loop().create_task(self._close_background())
        except RuntimeError:
            pass

    async def _close_background(self) -> None:
        try:
            await self.close()
        except Exception as exc:
            logger.debug("Apple Speech STT background close failed: %s", exc)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "cancel", **_apple_speech_control_fields()}))
            await self._ws.close()
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if str(payload.get("type") or "") == "status":
                    self.last_status = payload
                    continue
                if str(payload.get("type") or "") == "started":
                    self._started_event.set()
                    continue
                if str(payload.get("type") or "") == "error":
                    detail = str(payload.get("detail") or payload)
                    logger.error("Apple Speech STT error: %s", detail)
                    if not self._started_event.is_set():
                        self._start_error = detail
                        self._started_event.set()
                    self._done_event.set()
                    continue
                await self._handle_event(_parse_apple_stt_event(payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                logger.error("Apple Speech STT receive error: %s", exc)
                if not self._started_event.is_set():
                    self._start_error = str(exc)
                    self._started_event.set()
                self._done_event.set()

    async def _handle_event(self, event: AppleSpeechEvent) -> None:
        self._events_seen += 1
        if event.text:
            self._latest_text = event.text
            if event.is_final:
                self._final_seen += 1
            else:
                self._interim_seen += 1
            if self._on_transcript is not None:
                await self._on_transcript(event.text, event.is_final)
        if event.is_terminal:
            self._done_event.set()
