"""PyAudio capture and playback helpers for the satellite."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

from .config import SatelliteConfig
from .protocol import DEFAULT_TTS_SAMPLE_RATE, INPUT_CHANNELS, INPUT_FRAME_SAMPLES, INPUT_SAMPLE_RATE

logger = logging.getLogger(__name__)

PlaybackEndCallback = Callable[[], None]
_PLAYBACK_SILENCE_CHUNK_S = 0.05
_MAX_PLAYBACK_IDLE_SILENCE_S = 5.0
# Mono f32 at 24kHz is ~96 KB/s, so this ceiling is a memory guard against a
# runaway producer, not a normal-answer limit. At 300s it is ~28 MB, safe even
# on a 512 MB Pi Zero 2 W while covering any realistic spoken answer.
_MAX_BUFFERED_PLAYBACK_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class AudioDevice:
    index: int
    name: str
    input_channels: int
    output_channels: int
    default_sample_rate: float


@dataclass(frozen=True, slots=True)
class PlaybackChunk:
    audio: bytes
    sample_rate: int

    @property
    def duration_s(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return (len(self.audio) / 4) / self.sample_rate


def _import_pyaudio():
    try:
        import pyaudio  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "PyAudio is not installed. Install PortAudio/ALSA headers first, then run `uv sync`."
        ) from exc
    return pyaudio


def pyaudio_available() -> bool:
    try:
        _import_pyaudio()
    except RuntimeError:
        return False
    return True


def list_devices() -> list[AudioDevice]:
    pyaudio = _import_pyaudio()
    audio = pyaudio.PyAudio()
    try:
        devices: list[AudioDevice] = []
        for index in range(audio.get_device_count()):
            info = audio.get_device_info_by_index(index)
            devices.append(
                AudioDevice(
                    index=index,
                    name=str(info.get("name") or ""),
                    input_channels=int(info.get("maxInputChannels") or 0),
                    output_channels=int(info.get("maxOutputChannels") or 0),
                    default_sample_rate=float(info.get("defaultSampleRate") or 0),
                )
            )
        return devices
    finally:
        audio.terminate()


def print_devices() -> None:
    if pyaudio_available():
        for device in list_devices():
            flags = []
            if device.input_channels:
                flags.append(f"in:{device.input_channels}")
            if device.output_channels:
                flags.append(f"out:{device.output_channels}")
            print(
                f"{device.index}: {device.name} ({', '.join(flags) or 'no audio'}, "
                f"default {device.default_sample_rate:g}Hz)"
            )
        return

    for command in (("arecord", "-L"), ("aplay", "-L")):
        if shutil.which(command[0]) is None:
            print(f"{command[0]} not found")
            continue
        print(f"\n$ {' '.join(command)}")
        result = subprocess.run(command, check=False, text=True, capture_output=True)
        print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip())


def resolve_device_index(audio, selector: int | str | None, *, output: bool) -> int | None:
    if selector is None:
        return None
    if isinstance(selector, int):
        return selector

    needle = selector.lower()
    for index in range(audio.get_device_count()):
        info = audio.get_device_info_by_index(index)
        channels_key = "maxOutputChannels" if output else "maxInputChannels"
        if int(info.get(channels_key) or 0) <= 0:
            continue
        if needle in str(info.get("name") or "").lower():
            return index
    raise ValueError(f"Audio device matching {selector!r} was not found")


class PlaybackBuffer:
    """Thread-safe float32 playback buffer with one drain notification per batch."""

    def __init__(self, on_playback_end: PlaybackEndCallback) -> None:
        self._on_playback_end = on_playback_end
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._batch_active = False

    @property
    def has_active_audio(self) -> bool:
        with self._lock:
            return self._batch_active or bool(self._buffer)

    def enqueue(self, audio: bytes) -> None:
        if not audio:
            return
        with self._lock:
            self._buffer.extend(audio)
            self._batch_active = True

    def stop(self) -> bool:
        with self._lock:
            had_audio = self._batch_active or bool(self._buffer)
            self._buffer.clear()
            self._batch_active = False
            return had_audio

    def read(self, byte_count: int) -> bytes:
        should_notify = False
        with self._lock:
            if self._buffer:
                chunk = bytes(self._buffer[:byte_count])
                del self._buffer[:byte_count]
                if len(chunk) < byte_count:
                    chunk += bytes(byte_count - len(chunk))
                if not self._buffer and self._batch_active:
                    self._batch_active = False
                    should_notify = True
            else:
                chunk = bytes(byte_count)
                if self._batch_active:
                    self._batch_active = False
                    should_notify = True

        if should_notify:
            self._on_playback_end()
        return chunk


class AudioIO:
    """Owns local microphone capture and speaker playback streams."""

    def __init__(
        self,
        config: SatelliteConfig,
        *,
        mic_queue: asyncio.Queue[bytes],
        on_playback_end: PlaybackEndCallback,
    ) -> None:
        self._config = config
        self._mic_queue = mic_queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pyaudio = _import_pyaudio()
        self._audio = self._pyaudio.PyAudio()
        self._input_stream = None
        self._output_stream = None
        self._output_sample_rate = DEFAULT_TTS_SAMPLE_RATE
        self._playback = PlaybackBuffer(on_playback_end)
        self.input_overflows = 0
        self.output_underflows = 0

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        input_device = resolve_device_index(self._audio, self._config.input_device, output=False)
        self._input_stream = self._audio.open(
            format=self._pyaudio.paInt16,
            channels=INPUT_CHANNELS,
            rate=INPUT_SAMPLE_RATE,
            input=True,
            input_device_index=input_device,
            frames_per_buffer=self._config.input_frame_samples or INPUT_FRAME_SAMPLES,
            stream_callback=self._input_callback,
        )
        self._input_stream.start_stream()
        logger.info("Started microphone stream at %sHz device=%s", INPUT_SAMPLE_RATE, input_device)
        self._ensure_output_stream(DEFAULT_TTS_SAMPLE_RATE)

    def close(self) -> None:
        for stream in (self._input_stream, self._output_stream):
            if stream is None:
                continue
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                logger.debug("Ignoring audio stream close failure", exc_info=True)
        self._audio.terminate()

    def enqueue_playback(self, audio: bytes, *, sample_rate: int) -> None:
        self._ensure_output_stream(sample_rate)
        self._playback.enqueue(
            duplicate_float32_channels(audio, channels=max(1, self._config.playback_channels))
        )

    def begin_playback_stream(self) -> None:
        return None

    def finish_playback_stream(self) -> None:
        return None

    def stop_playback(self) -> bool:
        return self._playback.stop()

    def _ensure_output_stream(self, sample_rate: int) -> None:
        if self._output_stream is not None and sample_rate == self._output_sample_rate:
            return
        if self._output_stream is not None:
            self._output_stream.stop_stream()
            self._output_stream.close()

        output_device = resolve_device_index(self._audio, self._config.output_device, output=True)
        self._output_sample_rate = sample_rate
        self._output_stream = self._audio.open(
            format=self._pyaudio.paFloat32,
            channels=max(1, self._config.playback_channels),
            rate=sample_rate,
            output=True,
            output_device_index=output_device,
            frames_per_buffer=0,
            stream_callback=self._output_callback,
        )
        self._output_stream.start_stream()
        logger.info("Started speaker stream at %sHz device=%s", sample_rate, output_device)

    def _input_callback(self, in_data, frame_count, time_info, status):
        if status:
            logger.debug("Input stream status: %s", status)
        loop = self._loop
        if loop is not None:
            chunk = bytes(in_data)

            def put_chunk() -> None:
                if self._mic_queue.full():
                    self.input_overflows += 1
                    return
                self._mic_queue.put_nowait(chunk)

            loop.call_soon_threadsafe(put_chunk)
        return (None, self._pyaudio.paContinue)

    def _output_callback(self, in_data, frame_count, time_info, status):
        if status:
            self.output_underflows += 1
            logger.debug("Output stream status: %s", status)
        byte_count = frame_count * 4 * max(1, self._config.playback_channels)
        return (self._playback.read(byte_count), self._pyaudio.paContinue)


def _alsa_device_arg(selector: int | str | None) -> str:
    if selector is None:
        return "default"
    if isinstance(selector, int):
        return f"plughw:{selector},0"
    return selector


def select_interleaved_channel(audio: bytes, *, channels: int, channel_index: int) -> bytes:
    """Return one s16le channel from interleaved PCM."""
    if channels <= 1:
        return audio
    if channel_index < 0 or channel_index >= channels:
        raise ValueError(f"channel_index must be in [0, {channels - 1}]")

    frame_width = channels * 2
    selected = bytearray(len(audio) // channels)
    out = 0
    for frame_start in range(0, len(audio) - frame_width + 1, frame_width):
        sample_start = frame_start + channel_index * 2
        selected[out : out + 2] = audio[sample_start : sample_start + 2]
        out += 2
    return bytes(selected[:out])


def duplicate_float32_channels(audio: bytes, *, channels: int) -> bytes:
    """Expand mono f32le PCM into interleaved multi-channel PCM."""
    if channels <= 1 or not audio:
        return audio

    sample_width = 4
    complete_samples = len(audio) // sample_width
    output = bytearray(complete_samples * sample_width * channels)
    out = 0
    for sample_start in range(0, complete_samples * sample_width, sample_width):
        sample = audio[sample_start : sample_start + sample_width]
        for _ in range(channels):
            output[out : out + sample_width] = sample
            out += sample_width
    return bytes(output[:out])


class AlsaProcessAudioIO:
    """Audio adapter backed by arecord/aplay, useful on small Pis without PyAudio wheels."""

    def __init__(
        self,
        config: SatelliteConfig,
        *,
        mic_queue: asyncio.Queue[bytes],
        on_playback_end: PlaybackEndCallback,
    ) -> None:
        self._config = config
        self._mic_queue = mic_queue
        self._on_playback_end = on_playback_end
        self._capture_task: asyncio.Task | None = None
        self._playback_task: asyncio.Task | None = None
        self._capture_process: asyncio.subprocess.Process | None = None
        self._playback_process: asyncio.subprocess.Process | None = None
        self._playback_queue: asyncio.Queue[PlaybackChunk] = asyncio.Queue()
        self._playback_finish_event = asyncio.Event()
        self._playback_active = False
        self._expecting_more_playback = False
        self._queued_audio_seconds = 0.0
        self._playback_queue_peak_seconds = 0.0
        self.input_overflows = 0
        self.output_underflows = 0
        self.playback_dropped_chunks = 0
        self.playback_circuit_breaks = 0
        self.aplay_start_count = 0
        self.silence_fill_chunks = 0
        self.capture_restarts = 0
        self.playback_failures = 0

    def start(self) -> None:
        if shutil.which("arecord") is None or shutil.which("aplay") is None:
            raise RuntimeError("ALSA backend requires `arecord` and `aplay` from alsa-utils")
        self._capture_task = asyncio.create_task(self._capture_loop(), name="alsa-capture")
        self._playback_task = asyncio.create_task(self._playback_loop(), name="alsa-playback")
        logger.info("Started ALSA process audio backend")

    def close(self) -> None:
        for task in (self._capture_task, self._playback_task):
            if task is not None:
                task.cancel()
        for process in (self._capture_process, self._playback_process):
            self._terminate_process(process)

    def enqueue_playback(self, audio: bytes, *, sample_rate: int) -> None:
        if not audio:
            return
        chunk = PlaybackChunk(audio=audio, sample_rate=sample_rate)
        if self._queued_audio_seconds + chunk.duration_s > _MAX_BUFFERED_PLAYBACK_SECONDS:
            # Drop only the incoming tail; never discard already-buffered audio or
            # tear down the active stream, which would leave a start+end with a
            # missing middle. This bounds memory while keeping playback contiguous.
            self.playback_circuit_breaks += 1
            self.playback_dropped_chunks += 1
            if self.playback_dropped_chunks == 1 or self.playback_circuit_breaks % 50 == 0:
                logger.warning(
                    "Playback buffer at %.0fs ceiling; dropping incoming audio tail",
                    _MAX_BUFFERED_PLAYBACK_SECONDS,
                )
            return
        self._queued_audio_seconds += chunk.duration_s
        self._playback_queue_peak_seconds = max(self._playback_queue_peak_seconds, self._queued_audio_seconds)
        self._playback_queue.put_nowait(chunk)

    def begin_playback_stream(self) -> None:
        self._expecting_more_playback = True
        self._playback_finish_event.clear()

    def finish_playback_stream(self) -> None:
        self._expecting_more_playback = False
        self._playback_finish_event.set()

    def stop_playback(self) -> bool:
        had_audio = self._playback_active or not self._playback_queue.empty()
        while True:
            try:
                chunk = self._playback_queue.get_nowait()
                self._queued_audio_seconds = max(0.0, self._queued_audio_seconds - chunk.duration_s)
                self._playback_queue.task_done()
            except asyncio.QueueEmpty:
                break
        self.finish_playback_stream()
        self._terminate_process(self._playback_process)
        self._playback_process = None
        self._playback_active = False
        return had_audio

    async def _capture_loop(self) -> None:
        device = _alsa_device_arg(self._config.input_device)
        input_channels = max(1, self._config.input_channels)
        frame_bytes = (self._config.input_frame_samples or INPUT_FRAME_SAMPLES) * 2 * input_channels
        command = [
            "arecord",
            "-D",
            device,
            "-r",
            str(INPUT_SAMPLE_RATE),
            "-c",
            str(input_channels),
            "-f",
            "S16_LE",
            "-t",
            "raw",
            "-q",
        ]
        while True:
            logger.info("Starting arecord: %s", " ".join(command))
            self._capture_process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert self._capture_process.stdout is not None
            try:
                while True:
                    chunk = await self._capture_process.stdout.readexactly(frame_bytes)
                    chunk = select_interleaved_channel(
                        chunk,
                        channels=input_channels,
                        channel_index=self._config.input_channel_index,
                    )
                    if self._mic_queue.full():
                        self.input_overflows += 1
                        continue
                    self._mic_queue.put_nowait(chunk)
            except asyncio.IncompleteReadError:
                self.capture_restarts += 1
                logger.warning("arecord stopped; restarting")
            finally:
                await self._collect_stderr(self._capture_process, "arecord")
                self._terminate_process(self._capture_process)
                await asyncio.sleep(1)

    async def _playback_loop(self) -> None:
        while True:
            first_chunk = await self._get_playback_chunk()
            self._playback_active = True
            try:
                await self._play_stream(first_chunk, first_from_queue=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.playback_failures += 1
                logger.exception("ALSA playback batch failed; continuing playback loop")
            finally:
                self._playback_active = False
                self._on_playback_end()

    async def _get_playback_chunk(self) -> PlaybackChunk:
        chunk = await self._playback_queue.get()
        self._queued_audio_seconds = max(0.0, self._queued_audio_seconds - chunk.duration_s)
        return chunk

    def _get_playback_chunk_nowait(self) -> PlaybackChunk:
        chunk = self._playback_queue.get_nowait()
        self._queued_audio_seconds = max(0.0, self._queued_audio_seconds - chunk.duration_s)
        return chunk

    async def _play_stream(self, first_chunk: PlaybackChunk, *, first_from_queue: bool = False) -> None:
        sample_rate = first_chunk.sample_rate
        process: asyncio.subprocess.Process | None = None
        chunk: PlaybackChunk | None = first_chunk
        chunk_from_queue = first_from_queue
        try:
            process = await self._start_aplay(sample_rate)
            self._playback_process = process
            while True:
                if chunk is not None:
                    if chunk.sample_rate != sample_rate:
                        await self._finish_aplay(process)
                        self._playback_process = None
                        process = await self._start_aplay(chunk.sample_rate)
                        self._playback_process = process
                        sample_rate = chunk.sample_rate
                    try:
                        await self._write_playback(process, chunk.audio)
                    finally:
                        if chunk_from_queue:
                            self._playback_queue.task_done()
                            chunk_from_queue = False

                chunk = await self._next_playback_chunk(process, sample_rate)
                chunk_from_queue = chunk is not None
                if chunk is None:
                    break
            await self._finish_aplay(process)
        finally:
            if chunk_from_queue:
                self._playback_queue.task_done()
            if process is not None:
                self._terminate_process(process)
                if self._playback_process is process:
                    self._playback_process = None

    async def _next_playback_chunk(
        self,
        process: asyncio.subprocess.Process,
        sample_rate: int,
    ) -> PlaybackChunk | None:
        idle_s = 0.0
        while True:
            try:
                return self._get_playback_chunk_nowait()
            except asyncio.QueueEmpty:
                pass

            if not self._expecting_more_playback or self._playback_finish_event.is_set():
                return None
            if idle_s >= _MAX_PLAYBACK_IDLE_SILENCE_S:
                logger.warning("Playback stream idle for %.1fs; draining current aplay", idle_s)
                return None

            try:
                chunk = await asyncio.wait_for(
                    self._get_playback_chunk(),
                    timeout=_PLAYBACK_SILENCE_CHUNK_S,
                )
                return chunk
            except asyncio.TimeoutError:
                if not self._expecting_more_playback or self._playback_finish_event.is_set():
                    return None
                await self._write_playback(process, self._silence_pcm(sample_rate, _PLAYBACK_SILENCE_CHUNK_S))
                self.silence_fill_chunks += 1
                idle_s += _PLAYBACK_SILENCE_CHUNK_S

    def _silence_pcm(self, sample_rate: int, duration_s: float) -> bytes:
        frames = max(1, int(sample_rate * duration_s))
        return bytes(frames * 4)

    async def _play_contiguous_batch(self, first_audio: bytes, sample_rate: int) -> None:
        await self._play_stream(PlaybackChunk(audio=first_audio, sample_rate=sample_rate))

    async def _start_aplay(self, sample_rate: int) -> asyncio.subprocess.Process:
        device = _alsa_device_arg(self._config.output_device)
        playback_channels = max(1, self._config.playback_channels)
        command = [
            "aplay",
            "-D",
            device,
            "-r",
            str(sample_rate),
            "-c",
            str(playback_channels),
            "-f",
            "FLOAT_LE",
            "-t",
            "raw",
            "-q",
        ]
        self.aplay_start_count += 1
        logger.info("Starting aplay: %s", " ".join(command))
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _write_playback(self, process: asyncio.subprocess.Process, audio: bytes) -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(
                duplicate_float32_channels(audio, channels=max(1, self._config.playback_channels))
            )
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("aplay pipe closed while writing playback")
            raise

    async def _finish_aplay(self, process: asyncio.subprocess.Process) -> None:
        if process.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.close()
                await process.stdin.wait_closed()
        await process.wait()
        await self._collect_stderr(process, "aplay")

    async def _collect_stderr(self, process: asyncio.subprocess.Process | None, name: str) -> None:
        if process is None or process.stderr is None:
            return
        with contextlib.suppress(Exception):
            stderr = await asyncio.wait_for(process.stderr.read(), timeout=0.1)
            if stderr:
                logger.warning("%s stderr: %s", name, stderr.decode(errors="replace").strip())

    def _terminate_process(self, process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGTERM)
