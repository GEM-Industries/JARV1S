"""Text-to-Speech backends: protocol + Cartesia, local Kokoro, and disabled."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Optional, Protocol

import numpy as np

from core import settings
from core.credentials.store import credential_store
from core.turns.sanitizer import sanitize_tts_text
from core.voice.helper_client import request_helper
from core.voice.config import (
    ResolvedVoiceConfig,
    resolve_voice_config,
    resolve_voice_config_sync,
)

logger = logging.getLogger(__name__)
MODEL_ID = "sonic-3.6"
LANGUAGE = "en"
SAMPLE_RATE = 24000
EMOTION = "calm"
WEBSOCKET_MAX_IDLE_S = 270.0
TRANSPORT_FRAME_MS = 80


class TTSBackend(Protocol):
    sample_rate: int

    @property
    def ready(self) -> bool: ...

    async def initialize(self) -> bool: ...

    def prepare_for_turn(self) -> None: ...

    def generate_audio_stream(
        self,
        text: str,
        context_id: Optional[str] = None,
        add_silence_ms: int = 0,
    ) -> AsyncIterator[bytes]: ...

    async def close(self) -> None: ...


class DisabledTTSService:
    """No spoken output — text replies only."""

    sample_rate = SAMPLE_RATE

    @property
    def ready(self) -> bool:
        return False

    async def initialize(self) -> bool:
        return False

    def prepare_for_turn(self) -> None:
        return

    async def generate_audio_stream(
        self,
        text: str,
        context_id: Optional[str] = None,
        add_silence_ms: int = 0,
    ) -> AsyncIterator[bytes]:
        del text, context_id, add_silence_ms
        if False:  # pragma: no cover - makes this an async generator
            yield b""

    async def close(self) -> None:
        return


class CartesiaTTSService:
    """Text-to-Speech via Cartesia with a persistent WebSocket."""

    def __init__(self, *, voice_id: str | None = None):
        self.language = LANGUAGE
        self.sample_rate = SAMPLE_RATE
        self._voice_id = (voice_id or "").strip() or None
        self._generation_config = {
            "emotion": EMOTION,
        }
        self._client = None
        self._ws = None
        self._ws_last_used_at: float | None = None
        self._initialized = False
        self._prepare_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._client is not None and self._initialized

    def _resolved_voice_id(self) -> str | None:
        if self._voice_id:
            return self._voice_id
        return resolve_voice_config_sync().cartesia_voice_id

    def prepare_for_turn(self) -> None:
        """Prepare TTS in the background while the user is speaking."""
        if self._prepare_task is not None and not self._prepare_task.done():
            return

        task = asyncio.create_task(self._prepare_for_turn())
        self._prepare_task = task
        task.add_done_callback(self._clear_prepare_task)

    def _clear_prepare_task(self, task: asyncio.Task[None]) -> None:
        if self._prepare_task is task:
            self._prepare_task = None

    async def _prepare_for_turn(self) -> None:
        started_at = time.monotonic()
        previous_ws = self._ws
        prepared = await self.initialize()
        logger.info(
            "Cartesia TTS turn preparation finished (prepared=%s reconnected=%s elapsed_ms=%.1f)",
            prepared,
            prepared and self._ws is not previous_ws,
            (time.monotonic() - started_at) * 1000,
        )

    async def initialize(self) -> bool:
        api_key = credential_store.get_stored_secret("CARTESIA_API_KEY")
        config = await resolve_voice_config()
        voice_id = self._voice_id or config.cartesia_voice_id
        if not api_key or not voice_id:
            return False

        async with self._init_lock:
            try:
                if self._client is None:
                    from cartesia import AsyncCartesia  # type: ignore[import-not-found]

                    self._client = AsyncCartesia(api_key=api_key)

                previous_ws = self._ws
                ws = await self._ensure_websocket()
                if ws is not previous_ws:
                    async with self._lock:
                        await self.warmup()

                self._initialized = True
                logger.info(
                    "Cartesia TTS ready (model=%s voice=%s reconnected=%s)",
                    MODEL_ID,
                    voice_id,
                    ws is not previous_ws,
                )
                return True
            except Exception as e:
                logger.error(f"Failed to initialize Cartesia TTS: {e}")
                self._initialized = False
                self._client = None
                self._ws = None
                self._ws_last_used_at = None
                return False

    async def warmup(self) -> None:
        """Send a minimal request to pre-warm Cartesia's server-side voice model."""
        if not self._client:
            return
        try:
            voice_id = self._resolved_voice_id()
            if not voice_id:
                return
            ws = await self._ensure_websocket()
            output = await ws.send(
                model_id=MODEL_ID,
                transcript="Hello.",
                voice={"mode": "id", "id": voice_id},
                language=self.language,
                context_id="warmup",
                stream=True,
                max_buffer_delay_ms=0,
                generation_config=self._generation_config,
                output_format={
                    "container": "raw",
                    "encoding": "pcm_f32le",
                    "sample_rate": self.sample_rate,
                },
            )
            async for _ in output:
                pass  # discard audio
            self._ws_last_used_at = time.monotonic()
            logger.info("Cartesia TTS warmup complete")
        except Exception as e:
            logger.warning("Cartesia TTS warmup failed (non-fatal): %s", e)

    async def _ensure_websocket(self):
        """Ensure the WebSocket connection is open and active."""
        idle_expired = (
            self._ws is not None
            and self._ws_last_used_at is not None
            and time.monotonic() - self._ws_last_used_at >= WEBSOCKET_MAX_IDLE_S
        )
        if idle_expired:
            logger.info("Replacing idle Cartesia WebSocket before generation")
            await self._close_websocket()

        needs_connect = (
            self._ws is None or
            (hasattr(self._ws, "websocket") and self._ws.websocket.closed)
        )

        if needs_connect:
            if self._ws is not None:
                logger.warning("Cartesia WebSocket was closed. Reconnecting...")
            else:
                logger.info("Opening new Cartesia WebSocket connection...")
            self._ws = await self._client.tts.websocket()
            self._ws_last_used_at = time.monotonic()

        return self._ws

    async def _close_websocket(self) -> None:
        ws, self._ws = self._ws, None
        self._ws_last_used_at = None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception as exc:
            logger.debug("Failed to close Cartesia WebSocket: %s", exc)

    async def close(self):
        prepare_task = self._prepare_task
        if (
            prepare_task is not None
            and prepare_task is not asyncio.current_task()
            and not prepare_task.done()
        ):
            prepare_task.cancel()
            try:
                await prepare_task
            except asyncio.CancelledError:
                pass
        self._prepare_task = None
        self._initialized = False
        try:
            await self._close_websocket()
        finally:
            if self._client:
                await self._client.close()
                self._client = None

    def _apply_fadeout(self, audio_bytes: bytes, duration_ms: int = 20) -> bytes:
        """Apply a linear fade-out to prevent clicks. Assumes float32 input."""
        try:
            audio = np.frombuffer(audio_bytes, dtype=np.float32).copy()
            fade_samples = min(int(self.sample_rate * duration_ms / 1000), len(audio))
            if fade_samples > 0:
                audio[-fade_samples:] *= np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
            return audio.tobytes()
        except Exception as e:
            logger.warning(f"Failed to apply fadeout: {e}")
            return audio_bytes

    async def generate_audio_stream(
        self,
        text: str,
        context_id: Optional[str] = None,
        add_silence_ms: int = 0,
    ) -> AsyncIterator[bytes]:
        """Stream audio for a text segment using the persistent WebSocket."""
        text = sanitize_tts_text(text)
        if not self._client or not text.strip():
            return

        prepare_task = self._prepare_task
        if (
            prepare_task is not None
            and prepare_task is not asyncio.current_task()
            and not prepare_task.done()
        ):
            await prepare_task

        async with self._lock:
            voice_id = self._resolved_voice_id()
            if not voice_id:
                return

            for attempt in range(2):
                audio_yielded = False
                try:
                    ws = await self._ensure_websocket()
                    output = await ws.send(
                        model_id=MODEL_ID,
                        transcript=text,
                        voice={
                            "mode": "id",
                            "id": voice_id,
                        },
                        language=self.language,
                        context_id=context_id,
                        stream=True,
                        max_buffer_delay_ms=0,
                        generation_config=self._generation_config,
                        output_format={
                            "container": "raw",
                            "encoding": "pcm_f32le",
                            "sample_rate": self.sample_rate,
                        },
                    )

                    prev_chunk = None
                    async for chunk in output:
                        audio = None
                        if hasattr(chunk, "audio"):
                            audio = chunk.audio
                        elif isinstance(chunk, dict) and "audio" in chunk:
                            audio = chunk["audio"]

                        if audio is None:
                            continue

                        if prev_chunk is not None:
                            audio_yielded = True
                            yield prev_chunk
                        prev_chunk = audio

                    if prev_chunk is not None:
                        audio_yielded = True
                        yield self._apply_fadeout(prev_chunk, duration_ms=10)

                    self._ws_last_used_at = time.monotonic()
                    if add_silence_ms > 0:
                        silence_samples = int(self.sample_rate * add_silence_ms / 1000)
                        if silence_samples > 0:
                            yield np.zeros(silence_samples, dtype=np.float32).tobytes()
                    return
                except asyncio.CancelledError:
                    await self._close_websocket()
                    raise
                except Exception as exc:
                    await self._close_websocket()
                    if attempt == 0 and not audio_yielded:
                        logger.warning(
                            "Cartesia stream failed before audio; reconnecting once: %s",
                            exc,
                        )
                        continue
                    logger.error("Cartesia Streaming Error: %s", exc)
                    raise


def _local_tts_control_fields() -> dict[str, str]:
    token = (settings.VOICE.local_tts_token or "").strip()
    return {"token": token} if token else {}


class LocalTTSHelperClient:
    """Control-plane client for local Kokoro TTS readiness."""

    def __init__(self, *, url: str | None = None) -> None:
        self.url = url or settings.VOICE.local_tts_url

    async def status(self) -> dict:
        return await self._request(
            "status",
            reply_timeout_s=settings.VOICE.local_tts_connect_timeout,
        )

    async def warm(self, *, voice: str) -> dict:
        return await self._request("warm", reply_timeout_s=60.0, voice=voice)

    async def _request(
        self,
        message_type: str,
        *,
        reply_timeout_s: float,
        **fields: object,
    ) -> dict:
        return await request_helper(
            self.url,
            message_type,
            fields={**_local_tts_control_fields(), **fields},
            connect_timeout_s=settings.VOICE.local_tts_connect_timeout,
            reply_timeout_s=reply_timeout_s,
        )


class LocalTTSService:
    """On-device Kokoro TTS via the supervised Host helper."""

    sample_rate = SAMPLE_RATE

    def __init__(
        self,
        *,
        url: str | None = None,
        voice_id: str | None = None,
    ) -> None:
        self.url = (url or settings.VOICE.local_tts_url).strip()
        self._voice_id = (voice_id or "").strip() or None
        self._initialized = False
        self._prepare_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._initialized and bool(self.url)

    def _resolved_voice_id(self) -> str:
        if self._voice_id:
            return self._voice_id
        return resolve_voice_config_sync().local_voice_id

    def prepare_for_turn(self) -> None:
        if self._prepare_task is not None and not self._prepare_task.done():
            return
        task = asyncio.create_task(self._prepare_for_turn())
        self._prepare_task = task
        task.add_done_callback(self._clear_prepare_task)

    def _clear_prepare_task(self, task: asyncio.Task[None]) -> None:
        if self._prepare_task is task:
            self._prepare_task = None

    async def _prepare_for_turn(self) -> None:
        await self.initialize()

    async def initialize(self) -> bool:
        if not self.url:
            self._initialized = False
            return False
        try:
            client = LocalTTSHelperClient(url=self.url)
            status = await client.warm(voice=self._resolved_voice_id())
            self._initialized = bool(status.get("ready"))
            if self._initialized:
                logger.info(
                    "Local TTS ready (voice=%s url=%s)",
                    self._resolved_voice_id(),
                    self.url,
                )
            else:
                logger.warning(
                    "Local TTS helper not ready: %s",
                    status.get("detail") or status.get("state"),
                )
            return self._initialized
        except Exception as exc:
            logger.error("Failed to initialize local TTS: %s", exc)
            self._initialized = False
            return False

    async def close(self) -> None:
        prepare_task = self._prepare_task
        if (
            prepare_task is not None
            and prepare_task is not asyncio.current_task()
            and not prepare_task.done()
        ):
            prepare_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await prepare_task
        self._prepare_task = None
        self._initialized = False

    async def generate_audio_stream(
        self,
        text: str,
        context_id: Optional[str] = None,
        add_silence_ms: int = 0,
    ) -> AsyncIterator[bytes]:
        text = sanitize_tts_text(text)
        if not text.strip() or not self.url:
            return

        prepare_task = self._prepare_task
        if (
            prepare_task is not None
            and prepare_task is not asyncio.current_task()
            and not prepare_task.done()
        ):
            await prepare_task

        async with self._lock:
            websockets = importlib.import_module("websockets")
            utterance_id = context_id or f"utt-{time.monotonic_ns()}"
            ws = await asyncio.wait_for(
                websockets.connect(self.url),
                timeout=settings.VOICE.local_tts_connect_timeout,
            )
            try:
                await ws.send(
                    json.dumps(
                        {
                            "type": "speak",
                            "utterance_id": utterance_id,
                            "text": text,
                            "voice": self._resolved_voice_id(),
                            "speed": 1.0,
                            **_local_tts_control_fields(),
                        }
                    )
                )
                while True:
                    message = await ws.recv()
                    if isinstance(message, (bytes, bytearray)):
                        if message:
                            yield bytes(message)
                        continue
                    if not isinstance(message, str):
                        continue
                    payload = json.loads(message)
                    if not isinstance(payload, dict):
                        continue
                    msg_type = payload.get("type")
                    if msg_type == "done":
                        break
                    if msg_type == "error":
                        raise RuntimeError(str(payload.get("detail") or "Local TTS failed"))
                if add_silence_ms > 0:
                    silence_samples = int(self.sample_rate * add_silence_ms / 1000)
                    if silence_samples > 0:
                        yield np.zeros(silence_samples, dtype=np.float32).tobytes()
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await ws.close()
                raise
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()


def build_tts_backend(config: ResolvedVoiceConfig) -> TTSBackend:
    if config.tts_provider == "cartesia":
        return CartesiaTTSService(voice_id=config.cartesia_voice_id)
    if config.tts_provider == "local":
        return LocalTTSService(url=config.local_tts_url, voice_id=config.local_voice_id)
    return DisabledTTSService()
