"""Async WebSocket client for the JARV1S satellite."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Protocol

import websockets  # type: ignore[import-not-found]

from .audio import AlsaProcessAudioIO, AudioIO, pyaudio_available
from .backend_url import validate_backend_url
from .config import DEFAULT_CONFIG_PATH, SatelliteConfig
from .identity import build_websocket_url, ensure_state_dir, load_or_create_node_id
from .led import build_led_controller
from .notification_audio import NotificationSoundPlayer, cue_audio, notification_audio
from .ticket import TicketAuthError, mint_ws_ticket
from .diagnostics import SatelliteDiagnostics
from .protocol import (
    INPUT_SAMPLE_RATE,
    INPUT_SAMPLE_WIDTH_BYTES,
    NODE_REPLACED_CLOSE_CODE,
    MessageType,
    decode_audio,
    ping_message,
    playback_end_message,
    user_audio_message,
    voice_activate_message,
)
from .setup_server import start_setup_server
from .wakeword import WakeDetector, build_wake_detector

logger = logging.getLogger(__name__)

# Host stages that need live mic PCM (VAD / barge-in / turn audio).
_ACTIVE_HOST_STAGES = frozenset(
    {
        "waking",
        "listening",
        "transcribing",
        "thinking",
        "composing_tool",
        "running_tool",
        "speaking",
    }
)


def reconnect_delay(attempt: int, *, base_delay_s: float, max_delay_s: float) -> float:
    return min(base_delay_s * max(1, attempt), max_delay_s)


class AudioAdapter(Protocol):
    def start(self) -> None: ...
    def close(self) -> None: ...
    def enqueue_playback(self, audio: bytes, *, sample_rate: int) -> None: ...
    def begin_playback_stream(self) -> None: ...
    def finish_playback_stream(self) -> None: ...
    def stop_playback(self) -> bool: ...


class SatelliteClient:
    """Thin transport client; the backend owns voice state and turn logic."""

    def __init__(self, config: SatelliteConfig, config_path: Path | None = None) -> None:
        validate_backend_url(config.backend_url)
        self._config = config
        self._config_path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
        ensure_state_dir(config)
        self._node_id = load_or_create_node_id(config)
        self._mic_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=config.mic_queue_max_chunks)
        self._playback_drained_queue: asyncio.Queue[int] = asyncio.Queue()
        self._audio: AudioAdapter | None = None
        self._last_pong_at = 0.0
        self._playback_generation = 0
        self._latest_drained_generation: int | None = None
        self._tts_end_generation: int | None = None
        self._playback_end_sent_generation: int | None = None
        self._playback_end_timeout_task: asyncio.Task | None = None
        self._suppress_playback_drain_generation: int | None = None
        self._playback_turn_id: str | None = None
        self._tts_end_turn_id: str | None = None
        self._stopping = False
        self._led = build_led_controller(config)
        self._owner_tool_cues_enabled = True
        self._diagnostics = SatelliteDiagnostics()
        self._last_input_overflows = 0
        self._last_capture_restarts = 0
        self._last_playback_dropped_chunks = 0
        self._last_playback_failures = 0
        self._reconnect_attempt = 0
        self._notification_player = NotificationSoundPlayer(
            lambda audio, sample_rate: self._audio_required().enqueue_playback(
                audio,
                sample_rate=sample_rate,
            )
        )
        self._wake: WakeDetector | None = None
        self._edge_wake_enabled = False
        self._streaming_to_host = False
        self._preroll: deque[bytes] = deque()
        self._preroll_bytes = 0
        self._preroll_max_bytes = max(
            1,
            int(INPUT_SAMPLE_RATE * INPUT_SAMPLE_WIDTH_BYTES * max(0.5, config.wake_preroll_seconds)),
        )
        self._setup_server = None
        self._init_edge_wake()

    def _init_edge_wake(self) -> None:
        if not self._config.edge_wakeword:
            return
        detector = build_wake_detector(
            self._config.resolved_wakeword_model_path,
            sensitivity=self._config.wakeword_sensitivity,
            consecutive_required=self._config.wakeword_patience,
            vad_threshold=self._config.wakeword_vad_threshold,
        )
        if detector is None:
            raise RuntimeError(
                "edge_wakeword is enabled but the detector could not load; "
                "check the model path and install the wakeword extra"
            )
            return
        self._wake = detector
        self._edge_wake_enabled = True
        logger.info(
            "Edge wakeword armed model=%s preroll_s=%.1f",
            self._config.resolved_wakeword_model_path,
            self._config.wake_preroll_seconds,
        )

    def _should_stream_to_host(self) -> bool:
        return not self._edge_wake_enabled or self._streaming_to_host

    def _set_streaming_to_host(self, active: bool) -> None:
        if self._streaming_to_host == active:
            return
        self._streaming_to_host = active
        if not active:
            self._preroll.clear()
            self._preroll_bytes = 0
            if self._wake is not None:
                self._wake.reset()
            logger.info("Edge wake: returned to local-only PASSIVE")

    def _note_preroll(self, chunk: bytes) -> None:
        self._preroll.append(chunk)
        self._preroll_bytes += len(chunk)
        while self._preroll and self._preroll_bytes > self._preroll_max_bytes:
            dropped = self._preroll.popleft()
            self._preroll_bytes -= len(dropped)

    def _take_preroll(self) -> list[bytes]:
        chunks = list(self._preroll)
        self._preroll.clear()
        self._preroll_bytes = 0
        return chunks

    @property
    def node_id(self) -> str:
        return self._node_id

    def _restart_after_pair(self) -> None:
        def _terminate() -> None:
            logger.info("Paired; restarting to load the new credential")
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Timer(0.4, _terminate).start()

    def _start_setup_listener(self) -> None:
        if self._setup_server is not None:
            return
        try:
            self._setup_server = start_setup_server(
                node_id=self._node_id,
                config_path=self._config_path,
                on_paired=self._restart_after_pair,
            )
        except OSError as exc:
            logger.warning("Setup listener unavailable: %s", exc)

    def _stop_setup_listener(self) -> None:
        server = self._setup_server
        if server is None:
            return
        self._setup_server = None
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            logger.debug("Setup listener stop failed", exc_info=True)

    async def run(self) -> None:
        if not self._config.device_token:
            self._start_setup_listener()
        self._start_audio()
        self._led.start()
        attempt = 0
        try:
            while not self._stopping:
                code = await self._run_once()
                if code == NODE_REPLACED_CLOSE_CODE:
                    logger.error("Connection replaced by another node_id=%s client; stopping", self._node_id)
                    return
                attempt += 1
                self._reconnect_attempt = attempt
                self._diagnostics.record(
                    "transport_transition",
                    severity="warning",
                    metadata={
                        "phase": "closed",
                        "code": code,
                        "attempts": attempt,
                        "recovery": "retry",
                    },
                )
                delay = reconnect_delay(
                    attempt,
                    base_delay_s=self._config.reconnect_base_delay_s,
                    max_delay_s=self._config.reconnect_max_delay_s,
                )
                await self._led.set_disconnected()
                logger.info("Reconnecting in %.1fs", delay)
                await asyncio.sleep(delay)
        finally:
            self._stop_setup_listener()
            self._audio_close()
            await self._led.stop()
            await self._led.set_disconnected()

    async def stop(self) -> None:
        self._stopping = True

    async def _run_once(self) -> int | None:
        ticket: str | None = None
        if self._config.device_token:
            try:
                ws_ticket = await asyncio.to_thread(
                    mint_ws_ticket,
                    self._config.backend_url,
                    self._config.device_token,
                )
                ticket = ws_ticket.ticket
            except TicketAuthError as exc:
                logger.warning("Failed to mint WebSocket ticket: %s", exc)
                self._start_setup_listener()
                await self._led.set_disconnected()
                return None
            except RuntimeError as exc:
                logger.warning("Failed to mint WebSocket ticket: %s", exc)
                await self._led.set_disconnected()
                return None

        url = build_websocket_url(self._config, self._node_id, ticket=ticket)
        safe_url = url.split("ticket=", 1)[0] + ("ticket=***" if "ticket=" in url else "")
        logger.info("Connecting satellite node_id=%s url=%s", self._node_id, safe_url)
        close_code: int | None = None

        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                logger.info("Connected to JARV1S backend")
                self._stop_setup_listener()
                await self._led.set_connected()
                self._last_pong_at = time.monotonic()
                recovered = self._reconnect_attempt > 0
                if recovered:
                    self._diagnostics.record(
                        "transport_transition",
                        severity="info",
                        metadata={
                            "phase": "recovered",
                            "attempts": self._reconnect_attempt,
                            "recovery": "reconnect",
                        },
                    )
                self._reconnect_attempt = 0
                self._streaming_to_host = False
                if self._wake is not None:
                    self._wake.reset()
                await self._flush_diagnostics(ws)
                if self._config.auto_activate:
                    self._set_streaming_to_host(True)
                    await self._send(ws, voice_activate_message().as_dict())

                tasks = {
                    asyncio.create_task(self._receive_loop(ws), name="receive"),
                    asyncio.create_task(self._mic_loop(ws), name="mic"),
                    asyncio.create_task(self._heartbeat_loop(ws), name="heartbeat"),
                    asyncio.create_task(self._playback_drained_loop(ws), name="playback-drained"),
                    asyncio.create_task(self._diagnostics_loop(ws), name="diagnostics"),
                }
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc:
                        raise exc
                self._cancel_playback_end_timeout()
        except websockets.exceptions.ConnectionClosed as exc:
            close_code = exc.code
            logger.warning("WebSocket closed code=%s reason=%s", exc.code, exc.reason)
        except OSError as exc:
            logger.warning("WebSocket connection failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Satellite connection failed")

        self._cancel_playback_end_timeout()
        await self._led.set_disconnected()
        return close_code

    def _start_audio(self) -> None:
        if self._audio is not None:
            return
        loop = asyncio.get_running_loop()

        def notify_playback_drained() -> None:
            loop.call_soon_threadsafe(self._handle_playback_drained)

        backend = self._config.audio_backend
        if backend == "auto":
            backend = "pyaudio" if pyaudio_available() else "alsa"
        if backend == "pyaudio":
            self._audio = AudioIO(self._config, mic_queue=self._mic_queue, on_playback_end=notify_playback_drained)
        elif backend == "alsa":
            self._audio = AlsaProcessAudioIO(
                self._config,
                mic_queue=self._mic_queue,
                on_playback_end=notify_playback_drained,
            )
        else:
            raise ValueError(f"Unsupported audio backend: {self._config.audio_backend}")
        logger.info("Using %s audio backend", backend)
        self._audio.start()

    def _handle_playback_drained(self) -> None:
        if self._suppress_playback_drain_generation == self._playback_generation:
            self._suppress_playback_drain_generation = None
            return
        self._playback_drained_queue.put_nowait(self._playback_generation)

    def _cancel_playback_end_timeout(self) -> None:
        task = self._playback_end_timeout_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self._playback_end_timeout_task = None

    def _audio_close(self) -> None:
        if self._audio is None:
            return
        self._audio.close()
        self._audio = None

    async def _mic_loop(self, ws: Any) -> None:
        while True:
            chunk = await self._mic_queue.get()

            if self._should_stream_to_host():
                await self._send(ws, user_audio_message(chunk).as_dict())
                continue

            if self._wake is None:
                continue

            self._note_preroll(chunk)
            hit = self._wake.process(chunk)
            if hit is None:
                continue

            self._set_streaming_to_host(True)
            logger.info("Edge wake accepted score=%.4f; flushing pre-roll then streaming", hit.score)
            await self._send(ws, voice_activate_message().as_dict())
            for buffered in self._take_preroll():
                await self._send(ws, user_audio_message(buffered).as_dict())
            await self._led.set_waking()

    async def _heartbeat_loop(self, ws: Any) -> None:
        while True:
            await self._send(ws, ping_message().as_dict())
            await asyncio.sleep(self._config.heartbeat_interval_s)
            if time.monotonic() - self._last_pong_at > self._config.heartbeat_timeout_s:
                logger.warning("Heartbeat timed out; closing socket")
                self._diagnostics.record(
                    "transport_transition",
                    severity="warning",
                    metadata={"phase": "heartbeat_timeout", "recovery": "force_reconnect"},
                )
                await self._flush_diagnostics(ws)
                await ws.close(code=4000, reason="heartbeat timeout")
                return

    async def _diagnostics_loop(self, ws: Any) -> None:
        while True:
            self._poll_audio_health()
            await self._flush_diagnostics(ws)
            await asyncio.sleep(2.0)

    def _poll_audio_health(self) -> None:
        audio = self._audio
        if audio is None:
            return
        overflows = int(getattr(audio, "input_overflows", 0) or 0)
        if overflows > self._last_input_overflows:
            self._diagnostics.record(
                "mic_interrupted",
                severity="warning",
                metadata={
                    "reason": "queue_overflow",
                    "overflows": overflows,
                    "delta": overflows - self._last_input_overflows,
                },
            )
            self._last_input_overflows = overflows
        restarts = int(getattr(audio, "capture_restarts", 0) or 0)
        if restarts > self._last_capture_restarts:
            self._diagnostics.record(
                "mic_interrupted",
                severity="warning",
                metadata={
                    "reason": "capture_restart",
                    "restarts": restarts,
                    "delta": restarts - self._last_capture_restarts,
                },
            )
            self._last_capture_restarts = restarts

        dropped_chunks = int(getattr(audio, "playback_dropped_chunks", 0) or 0)
        playback_failures = int(getattr(audio, "playback_failures", 0) or 0)
        if (
            dropped_chunks > self._last_playback_dropped_chunks
            or playback_failures > self._last_playback_failures
        ):
            self._diagnostics.record(
                "playback_failed",
                severity="error" if playback_failures > self._last_playback_failures else "warning",
                turn_id=self._playback_turn_id,
                metadata={
                    "reason": "audio_backend",
                    "dropped_chunks": dropped_chunks,
                    "process_failures": playback_failures,
                },
            )
            self._last_playback_dropped_chunks = dropped_chunks
            self._last_playback_failures = playback_failures

    async def _flush_diagnostics(self, ws: Any) -> None:
        while (payload := self._diagnostics.next_message()) is not None:
            await self._send(ws, payload)
            self._diagnostics.mark_sent(len(payload["data"]["events"]))

    async def _playback_drained_loop(self, ws: Any) -> None:
        while True:
            generation = await self._playback_drained_queue.get()
            if generation == 0:
                continue
            self._latest_drained_generation = generation
            if generation == self._tts_end_generation:
                await self._send_playback_end_once(ws, generation, reason="tts_end_and_drain")
            elif generation == self._playback_generation:
                self._arm_missing_tts_end_timeout(ws, generation)

    def _arm_missing_tts_end_timeout(self, ws: Any, generation: int) -> None:
        self._cancel_playback_end_timeout()
        self._playback_end_timeout_task = asyncio.create_task(
            self._missing_tts_end_timeout(ws, generation),
            name="playback-end-tts-end-timeout",
        )

    async def _missing_tts_end_timeout(self, ws: Any, generation: int) -> None:
        await asyncio.sleep(self._config.tts_end_timeout_s)
        if generation != self._playback_generation:
            return
        if generation != self._latest_drained_generation:
            return
        if generation == self._tts_end_generation:
            return
        self._diagnostics.record(
            "playback_failed",
            severity="warning",
            turn_id=self._playback_turn_id,
            metadata={"reason": "missing_tts_end_timeout", "generation": generation},
        )
        await self._send_playback_end_once(ws, generation, reason="missing_tts_end_timeout")

    async def _send_playback_end_once(self, ws: Any, generation: int, *, reason: str) -> None:
        if self._playback_end_sent_generation == generation:
            return
        self._playback_end_sent_generation = generation
        self._cancel_playback_end_timeout()
        self._notification_player.unduck()
        turn_id = self._tts_end_turn_id if generation == self._tts_end_generation else self._playback_turn_id
        logger.info(
            "Sending audio.playback_end reason=%s generation=%s turn_id=%s",
            reason,
            generation,
            turn_id,
        )
        await self._send(ws, playback_end_message(turn_id).as_dict())

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Ignoring non-JSON WebSocket message")
                continue
            await self._handle_message(ws, message)

    async def _handle_message(self, ws: Any, message: dict[str, Any]) -> None:
        msg_type = str(message.get("type") or "")
        data = message.get("data") if isinstance(message.get("data"), dict) else {}

        if msg_type == MessageType.SYSTEM_PONG.value:
            self._last_pong_at = time.monotonic()
            return

        if msg_type == MessageType.SYSTEM_CONNECT.value:
            identity = data.get("identity") or data.get("presence") or data
            logger.info("Backend accepted satellite identity: %s", identity)
            self._update_preferences(data)
            await self._update_led_context(data)
            return

        if msg_type == MessageType.SYSTEM_ERROR.value:
            logger.error("Backend error: %s", data or message)
            return

        if msg_type == MessageType.STATUS.value:
            stage = data.get("stage") or data.get("status")
            if stage:
                logger.info("Backend status: %s", stage)
                normalized = str(stage).strip().lower()
                if normalized == "idle":
                    self._set_streaming_to_host(False)
                elif normalized in _ACTIVE_HOST_STAGES:
                    self._set_streaming_to_host(True)
            await self._update_led_context(data, stage=str(stage) if stage else None)
            return

        if msg_type == MessageType.PREFERENCES_UPDATE.value:
            self._update_preferences(data)
            return

        if msg_type == MessageType.SPEECH_START.value:
            self._set_streaming_to_host(True)
            if data.get("barge_candidate"):
                logger.info("Barge-in candidate detected; keeping playback active")
                await self._led.set_stage("listening")
            elif data.get("wake_word"):
                logger.info("Wake word detected")
                await self._stop_local_audio(ws, reason="wake_word")
                await self._led.set_waking()
            else:
                await self._stop_local_audio(ws, reason="speech_start")
                await self._led.set_stage("listening")
            return

        if msg_type == MessageType.JARVIS_AUDIO.value:
            self._set_streaming_to_host(True)
            encoded = data.get("audio")
            if not isinstance(encoded, str):
                return
            sample_rate = int(data.get("sample_rate") or 24_000)
            turn_id = data.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                self._playback_turn_id = turn_id
            self._playback_generation += 1
            self._notification_player.duck()
            self._audio_required().begin_playback_stream()
            self._audio_required().enqueue_playback(decode_audio(encoded), sample_rate=sample_rate)
            return

        if msg_type == MessageType.TTS_END.value:
            turn_id = data.get("turn_id")
            self._tts_end_turn_id = (
                turn_id if isinstance(turn_id, str) and turn_id else self._playback_turn_id
            )
            self._tts_end_generation = self._playback_generation
            self._audio_required().finish_playback_stream()
            if self._latest_drained_generation == self._tts_end_generation:
                await self._send_playback_end_once(ws, self._tts_end_generation, reason="tts_end_after_drain")
            return

        if msg_type == MessageType.AUDIO_CUE.value:
            phase = data.get("phase")
            if isinstance(phase, str):
                self._play_audio_cue(phase)
            return

        if msg_type == MessageType.SYSTEM_STOP.value:
            await self._stop_local_audio(ws, reason="system_stop")
            return

        if msg_type == MessageType.NOTIFICATION_SOUND.value:
            sound = data.get("sound")
            logger.info("Notification sound requested: %s", sound)
            if not isinstance(sound, str):
                self._diagnostics.record(
                    "notification_failed",
                    severity="error",
                    metadata={"kind": "unknown", "reason": "missing_sound"},
                )
                return
            if notification_audio(sound) is None:
                self._diagnostics.record(
                    "notification_failed",
                    severity="error",
                    metadata={"kind": sound, "reason": "asset_missing"},
                )
            await self._notification_player.play(sound)

    async def _stop_local_audio(self, ws: Any, *, reason: str) -> None:
        await self._notification_player.stop()
        had_audio = self._audio_required().stop_playback()
        logger.info("Stopping local audio reason=%s had_audio=%s", reason, had_audio)
        if had_audio:
            self._playback_generation += 1
            self._suppress_playback_drain_generation = self._playback_generation
            await self._send_playback_end_once(ws, self._playback_generation, reason=reason)

    def _play_audio_cue(self, phase: str) -> None:
        if not self._config.tool_cues_enabled or not self._owner_tool_cues_enabled:
            return
        audio = cue_audio(phase)
        if audio is None:
            logger.info("Ignoring unsupported audio cue phase: %s", phase)
            return
        self._suppress_playback_drain_generation = self._playback_generation
        self._audio_required().enqueue_playback(audio.pcm, sample_rate=audio.sample_rate)

    def _update_preferences(self, data: dict[str, Any]) -> None:
        preferences = data.get("preferences")
        if not isinstance(preferences, dict):
            return
        audio = preferences.get("audio")
        if not isinstance(audio, dict):
            return
        self._owner_tool_cues_enabled = audio.get("tool_cues_enabled") is not False

    async def _update_led_context(self, data: dict[str, Any], *, stage: str | None = None) -> None:
        session = data.get("session") if isinstance(data.get("session"), dict) else {}
        attention = data.get("attention") if isinstance(data.get("attention"), dict) else {}
        soft_muted = session.get("soft_muted") if "soft_muted" in session else None
        attention_mode = attention.get("mode") if isinstance(attention.get("mode"), str) else None
        await self._led.update_context(
            stage=stage,
            soft_muted=bool(soft_muted) if soft_muted is not None else None,
            attention_mode=attention_mode,
        )

    def _audio_required(self) -> AudioAdapter:
        if self._audio is None:
            raise RuntimeError("Audio has not been started")
        return self._audio

    async def _send(self, ws: Any, payload: dict[str, Any]) -> None:
        await ws.send(json.dumps(payload, separators=(",", ":")))
