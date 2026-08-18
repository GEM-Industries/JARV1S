"""Local notification sound assets for satellite speakers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
import wave
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

logger = logging.getLogger(__name__)

_NOTIFICATION_SOUNDS = frozenset({"chime", "timer", "alarm"})
_CUE_SOUNDS = frozenset({"tool_start", "tool_done"})
_SUPPORTED = _NOTIFICATION_SOUNDS | _CUE_SOUNDS
_NORMAL_GAIN = 1.0
_DUCKED_GAIN = 0.15
_CUE_GAIN = 0.5


@dataclass(frozen=True, slots=True)
class NotificationAudio:
    pcm: bytes
    sample_rate: int


@lru_cache(maxsize=len(_SUPPORTED))
def notification_audio(sound: str) -> NotificationAudio | None:
    """Return mono f32le PCM for a notification sound asset."""
    if sound not in _NOTIFICATION_SOUNDS:
        return None
    return _sound_audio(sound)


def cue_audio(phase: str) -> NotificationAudio | None:
    """Return mono f32le PCM for a tool cue phase."""
    sound = f"tool_{phase}"
    if sound not in _CUE_SOUNDS:
        return None
    audio = _sound_audio(sound)
    if audio is None:
        return None
    return NotificationAudio(_scale_f32le(audio.pcm, _CUE_GAIN), audio.sample_rate)


@lru_cache(maxsize=len(_SUPPORTED))
def _sound_audio(sound: str) -> NotificationAudio | None:
    wav = _open_sound_asset(sound)
    if wav is None:
        logger.warning("Sound WAV asset missing: %s", sound)
        return None

    try:
        with wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except wave.Error:
        logger.warning("Sound WAV asset invalid: %s", sound, exc_info=True)
        return None

    if channels < 1 or sample_width not in (1, 2, 3, 4):
        logger.warning("Unsupported sound WAV format: %s", sound)
        return None

    return NotificationAudio(_wav_to_mono_f32le(frames, channels=channels, sample_width=sample_width), sample_rate)


def _open_sound_asset(sound: str):
    name = f"{sound}.wav"
    packaged = resources.files("jarvis_satellite").joinpath("assets", "sounds", name)
    try:
        return wave.open(packaged.open("rb"), "rb")
    except FileNotFoundError:
        return None


def _wav_to_mono_f32le(frames: bytes, *, channels: int, sample_width: int) -> bytes:
    pcm = bytearray((len(frames) // (sample_width * channels)) * 4)
    out = 0
    frame_width = sample_width * channels
    for frame_start in range(0, len(frames) - frame_width + 1, frame_width):
        total = 0.0
        for channel in range(channels):
            sample_start = frame_start + channel * sample_width
            sample = frames[sample_start : sample_start + sample_width]
            if sample_width == 1:
                total += (sample[0] - 128) / 128.0
            elif sample_width == 2:
                total += struct.unpack("<h", sample)[0] / 32768.0
            elif sample_width == 3:
                sign = b"\xff" if sample[2] & 0x80 else b"\x00"
                total += int.from_bytes(sample + sign, "little", signed=True) / 8388608.0
            else:
                total += struct.unpack("<i", sample)[0] / 2147483648.0
        pcm[out : out + 4] = struct.pack("<f", total / channels)
        out += 4
    return bytes(pcm)


def _scale_f32le(audio: bytes, gain: float) -> bytes:
    if gain == 1.0 or not audio:
        return audio
    samples = bytearray(len(audio))
    for offset in range(0, len(audio) - 3, 4):
        sample = struct.unpack("<f", audio[offset : offset + 4])[0]
        samples[offset : offset + 4] = struct.pack("<f", sample * gain)
    return bytes(samples)


class NotificationSoundPlayer:
    """Queue local notification sounds through the satellite audio backend."""

    def __init__(self, enqueue: Callable[[bytes, int], None]) -> None:
        self._enqueue = enqueue
        self._loop_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._ducked = False

    async def play(self, sound: str) -> None:
        if sound not in _NOTIFICATION_SOUNDS:
            logger.info("Ignoring unsupported notification sound: %s", sound)
            return
        await self.stop()
        if sound == "alarm":
            self._stop_event.clear()
            self._loop_task = asyncio.create_task(self._loop_alarm(), name="notification-alarm")
            return
        await self._play_once(sound)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        self._loop_task = None
        self._stop_event.clear()

    def duck(self) -> None:
        self._ducked = True

    def unduck(self) -> None:
        self._ducked = False

    async def _loop_alarm(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._play_once("alarm")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.8)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def _play_once(self, sound: str) -> None:
        audio = notification_audio(sound)
        if audio is None:
            logger.warning("Notification asset missing: %s", sound)
            return
        self._enqueue(
            _scale_f32le(audio.pcm, _DUCKED_GAIN if self._ducked else _NORMAL_GAIN),
            audio.sample_rate,
        )
