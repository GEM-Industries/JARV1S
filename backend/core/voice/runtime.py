"""Runtime STT/TTS provider switching."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Optional

from core.voice.config import ResolvedVoiceConfig, resolve_voice_config, resolve_voice_config_sync
from core.voice.stt_service import (
    AppleSpeechSTTService,
    CartesiaSTTService,
    STTBackend,
    STTCapabilities,
    StreamingSTTSession,
)
from core.voice.tts_service import TTSBackend, build_tts_backend

logger = logging.getLogger(__name__)


async def _close_quietly(backend: TTSBackend | None) -> None:
    if backend is None:
        return
    try:
        await backend.close()
    except Exception as exc:
        logger.debug("Failed to close TTS backend: %s", exc)


def build_stt_backend(config: ResolvedVoiceConfig) -> STTBackend:
    if config.stt_provider == "cartesia":
        return CartesiaSTTService()
    return AppleSpeechSTTService(url=config.apple_speech_url)


class SwitchableSTTBackend:
    """Delegates STT calls to the currently selected host provider.

    Active streaming sessions keep their provider. Config changes apply at the next
    turn boundary, which avoids tearing down user audio mid-utterance.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._backend: STTBackend | None = None
        self._signature: tuple[str, str | None] | None = None

    @property
    def capabilities(self) -> STTCapabilities:
        backend = self._backend
        if backend is not None:
            return backend.capabilities
        return build_stt_backend(resolve_voice_config_sync()).capabilities

    async def initialize(self) -> None:
        backend = await self._get_backend()
        await backend.initialize()

    async def transcribe_batched(self, audio_bytes: bytes) -> str:
        backend = await self._get_backend()
        return await backend.transcribe_batched(audio_bytes)

    async def start_streaming(self, on_transcript=None, on_turn_end=None) -> StreamingSTTSession | None:
        backend = await self._get_backend()
        return await backend.start_streaming(on_transcript=on_transcript, on_turn_end=on_turn_end)

    async def refresh(self) -> None:
        async with self._lock:
            config = await resolve_voice_config()
            self._backend = build_stt_backend(config)
            self._signature = config.signature
            logger.info("STT provider selected: %s", config.stt_provider)

    async def _get_backend(self) -> STTBackend:
        config = await resolve_voice_config()
        async with self._lock:
            if self._backend is None or self._signature != config.signature:
                self._backend = build_stt_backend(config)
                self._signature = config.signature
                logger.info("STT provider selected: %s", config.stt_provider)
            return self._backend


class SwitchableTTSBackend:
    """Delegates TTS calls to the currently selected output provider.

    A generate_audio_stream() call captures one backend for that complete sentence
    stream so a settings change cannot splice providers mid-sentence. Stale backends
    are closed after active generators finish.
    """

    sample_rate = 24000

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._backend: TTSBackend | None = None
        self._signature: tuple[str, str | None, str | None] | None = None
        self._active_streams = 0
        self._pending_close: list[TTSBackend] = []

    @property
    def ready(self) -> bool:
        backend = self._backend
        if backend is not None:
            return backend.ready
        return False

    def prepare_for_turn(self) -> None:
        backend = self._backend
        if backend is None:
            # Best-effort sync path while user speaks; initialize may still be racing.
            config = resolve_voice_config_sync()
            backend = build_tts_backend(config)
            self._backend = backend
            self._signature = config.tts_signature
        backend.prepare_for_turn()

    async def initialize(self) -> bool:
        backend = await self._get_backend()
        return await backend.initialize()

    async def generate_audio_stream(
        self,
        text: str,
        context_id: Optional[str] = None,
        add_silence_ms: int = 0,
    ) -> AsyncIterator[bytes]:
        backend = await self._get_backend()
        async with self._lock:
            self._active_streams += 1
        try:
            async for chunk in backend.generate_audio_stream(
                text,
                context_id=context_id,
                add_silence_ms=add_silence_ms,
            ):
                yield chunk
        finally:
            async with self._lock:
                self._active_streams = max(0, self._active_streams - 1)
                pending = [] if self._active_streams else list(self._pending_close)
                if self._active_streams == 0:
                    self._pending_close.clear()
            for stale in pending:
                await _close_quietly(stale)

    async def close(self) -> None:
        async with self._lock:
            targets = [self._backend, *self._pending_close]
            self._backend = None
            self._signature = None
            self._pending_close.clear()
            self._active_streams = 0
        for target in targets:
            await _close_quietly(target)

    async def refresh(self) -> None:
        config = await resolve_voice_config()
        await self.promote(build_tts_backend(config), config.tts_signature)

    async def promote(
        self,
        backend: TTSBackend,
        signature: tuple[str, str | None, str | None],
    ) -> None:
        """Install a backend, deferring disposal of the previous one until streams drain."""
        async with self._lock:
            stale = self._swap_locked(backend, signature)
        await _close_quietly(stale)

    async def _get_backend(self) -> TTSBackend:
        config = await resolve_voice_config()
        stale: TTSBackend | None = None
        async with self._lock:
            if self._backend is None or self._signature != config.tts_signature:
                stale = self._swap_locked(build_tts_backend(config), config.tts_signature)
            backend = self._backend
        await _close_quietly(stale)
        assert backend is not None
        return backend

    def _swap_locked(
        self,
        backend: TTSBackend,
        signature: tuple[str, str | None, str | None],
    ) -> TTSBackend | None:
        """Install `backend` under the held lock. Returns the previous backend to close
        now, or None when there is nothing to close (or disposal is deferred)."""
        previous = self._backend
        self._backend = backend
        self._signature = signature
        logger.info("TTS provider selected: %s", signature[0])
        if previous is None or previous is backend:
            return None
        if self._active_streams > 0:
            self._pending_close.append(previous)
            return None
        return previous


switchable_stt = SwitchableSTTBackend()
switchable_tts = SwitchableTTSBackend()
