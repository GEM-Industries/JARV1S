import struct
import wave
from pathlib import Path

import pytest

from jarvis_satellite.notification_audio import NotificationSoundPlayer, cue_audio, notification_audio


@pytest.fixture(autouse=True)
def sound_asset(monkeypatch, tmp_path):
    import jarvis_satellite.notification_audio as notification_audio_module

    path = tmp_path / "sound.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(struct.pack("<hhh", 0, 10_000, -10_000))

    def open_sound_asset(sound: str):
        if sound not in {"chime", "timer", "alarm", "tool_start"}:
            return None
        return wave.open(str(path), "rb")

    notification_audio.cache_clear()
    notification_audio_module._sound_audio.cache_clear()
    monkeypatch.setattr(notification_audio_module, "_open_sound_asset", open_sound_asset)
    yield
    notification_audio.cache_clear()
    notification_audio_module._sound_audio.cache_clear()


@pytest.mark.asyncio
async def test_play_chime_queues_pcm_once():
    queued: list[tuple[bytes, int]] = []

    player = NotificationSoundPlayer(lambda audio, sample_rate: queued.append((audio, sample_rate)))
    await player.play("chime")

    assert len(queued) == 1
    assert queued[0][0]
    assert queued[0][1] > 0


def test_notification_audio_loads_wav_asset_as_mono_float32_pcm():
    audio = notification_audio("timer")

    assert audio is not None
    assert audio.sample_rate > 0
    assert len(audio.pcm) % 4 == 0
    assert notification_audio("unknown") is None


def test_cue_audio_loads_24_bit_stereo_wav(monkeypatch, tmp_path):
    import jarvis_satellite.notification_audio as notification_audio_module

    path = tmp_path / "tool_start.wav"
    frames = bytearray()
    for left, right in ((0, 0), (1_000_000, -1_000_000), (-2_000_000, 2_000_000)):
        frames.extend(left.to_bytes(3, "little", signed=True))
        frames.extend(right.to_bytes(3, "little", signed=True))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(3)
        wav.setframerate(44_100)
        wav.writeframes(bytes(frames))

    def open_sound_asset(sound: str):
        if sound != "tool_start":
            return None
        return wave.open(str(path), "rb")

    notification_audio_module._sound_audio.cache_clear()
    monkeypatch.setattr(notification_audio_module, "_open_sound_asset", open_sound_asset)

    audio = cue_audio("start")

    assert audio is not None
    assert audio.sample_rate == 44_100
    assert len(audio.pcm) == 3 * 4
    assert cue_audio("unknown") is None
    notification_audio_module._sound_audio.cache_clear()


def test_shared_timer_asset_has_no_long_silent_tail():
    root = Path(__file__).resolve().parents[2]
    path = root / "frontend" / "public" / "sounds" / "timer.wav"
    with wave.open(str(path), "rb") as wav:
        assert wav.getnframes() / wav.getframerate() <= 3.1


@pytest.mark.asyncio
async def test_system_stop_stops_alarm_loop():
    player = NotificationSoundPlayer(lambda _audio, _sample_rate: None)
    player._loop_task = __import__("asyncio").create_task(__import__("asyncio").sleep(60))
    await player.stop()
    assert player._loop_task is None
