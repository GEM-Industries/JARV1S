import asyncio

import pytest

from jarvis_satellite.audio import (
    AlsaProcessAudioIO,
    PlaybackBuffer,
    PlaybackChunk,
    duplicate_float32_channels,
    select_interleaved_channel,
)
from jarvis_satellite.config import SatelliteConfig


def test_playback_buffer_sends_one_end_when_batch_drains():
    ended = 0

    def on_end() -> None:
        nonlocal ended
        ended += 1

    playback = PlaybackBuffer(on_end)
    playback.enqueue(b"12345678")

    assert playback.read(4) == b"1234"
    assert ended == 0
    assert playback.read(4) == b"5678"
    assert ended == 1
    assert playback.read(4) == b"\x00\x00\x00\x00"
    assert ended == 1


def test_playback_buffer_sends_one_end_when_partial_read_drains():
    ended = 0

    def on_end() -> None:
        nonlocal ended
        ended += 1

    playback = PlaybackBuffer(on_end)
    playback.enqueue(b"12")

    assert playback.read(4) == b"12\x00\x00"
    assert ended == 1


def test_playback_buffer_stop_reports_active_audio_without_callback():
    ended = 0

    def on_end() -> None:
        nonlocal ended
        ended += 1

    playback = PlaybackBuffer(on_end)
    playback.enqueue(b"1234")

    assert playback.stop() is True
    assert ended == 0
    assert playback.stop() is False


def test_empty_read_without_audio_does_not_send_playback_end():
    ended = 0

    def on_end() -> None:
        nonlocal ended
        ended += 1

    playback = PlaybackBuffer(on_end)

    assert playback.read(4) == b"\x00\x00\x00\x00"
    assert ended == 0


def test_select_interleaved_channel_extracts_asr_channel():
    # Two s16le stereo frames: (conference=1, asr=2), (conference=3, asr=4).
    interleaved = b"\x01\x00\x02\x00\x03\x00\x04\x00"

    assert select_interleaved_channel(interleaved, channels=2, channel_index=1) == b"\x02\x00\x04\x00"


def test_duplicate_float32_channels_keeps_left_reference_populated():
    # Two f32le samples, expanded to stereo as L/R pairs.
    mono = b"abcdwxyz"

    assert duplicate_float32_channels(mono, channels=2) == b"abcdabcdwxyzwxyz"


def test_alsa_playback_queue_is_lossless_for_bursts():
    audio = AlsaProcessAudioIO(
        SatelliteConfig(),
        mic_queue=asyncio.Queue(),
        on_playback_end=lambda: None,
    )

    audio.enqueue_playback(b"one", sample_rate=24_000)
    audio.enqueue_playback(b"two", sample_rate=24_000)
    audio.enqueue_playback(b"three", sample_rate=24_000)

    assert audio._playback_queue.qsize() == 3
    assert audio.playback_dropped_chunks == 0
    assert audio.playback_circuit_breaks == 0


def test_alsa_playback_circuit_breaker_drops_tail_without_discarding_buffer(monkeypatch):
    import jarvis_satellite.audio as audio_module

    # Ceiling allows one 0.05s chunk (4800 bytes mono f32 @ 24kHz) but not two.
    monkeypatch.setattr(audio_module, "_MAX_BUFFERED_PLAYBACK_SECONDS", 0.06)
    audio = AlsaProcessAudioIO(
        SatelliteConfig(),
        mic_queue=asyncio.Queue(),
        on_playback_end=lambda: None,
    )

    audio.enqueue_playback(bytes(4_800), sample_rate=24_000)
    audio.enqueue_playback(bytes(4_800), sample_rate=24_000)

    # The already-buffered chunk survives; only the incoming tail is dropped.
    assert audio._playback_queue.qsize() == 1
    assert audio.playback_dropped_chunks == 1
    assert audio.playback_circuit_breaks == 1
    assert audio._playback_active is False


@pytest.mark.asyncio
async def test_alsa_sample_rate_switch_marks_queue_item_done_once():
    ended = 0

    def on_end() -> None:
        nonlocal ended
        ended += 1

    audio = AlsaProcessAudioIO(
        SatelliteConfig(playback_end_settle_s=0.001),
        mic_queue=asyncio.Queue(),
        on_playback_end=on_end,
    )
    started_rates: list[int] = []
    written: list[bytes] = []

    async def fake_start_aplay(sample_rate: int):
        started_rates.append(sample_rate)
        return object()

    async def fake_write_playback(process, chunk: bytes) -> None:
        written.append(chunk)

    async def fake_finish_aplay(process) -> None:
        return None

    audio._start_aplay = fake_start_aplay
    audio._write_playback = fake_write_playback
    audio._finish_aplay = fake_finish_aplay
    audio._terminate_process = lambda process: None
    audio.enqueue_playback(b"second", sample_rate=16_000)

    await audio._play_contiguous_batch(b"first", 24_000)

    assert started_rates == [24_000, 16_000]
    assert written == [b"first", b"second"]
    assert ended == 0


@pytest.mark.asyncio
async def test_alsa_playback_keeps_single_aplay_open_across_short_gap(monkeypatch):
    import jarvis_satellite.audio as audio_module

    monkeypatch.setattr(audio_module, "_PLAYBACK_SILENCE_CHUNK_S", 0.001)
    monkeypatch.setattr(audio_module, "_MAX_PLAYBACK_IDLE_SILENCE_S", 0.05)
    ended = asyncio.Event()
    audio = AlsaProcessAudioIO(
        SatelliteConfig(),
        mic_queue=asyncio.Queue(),
        on_playback_end=ended.set,
    )
    started_rates: list[int] = []
    written: list[bytes] = []

    async def fake_start_aplay(sample_rate: int):
        started_rates.append(sample_rate)
        return object()

    async def fake_write_playback(process, chunk: bytes) -> None:
        written.append(chunk)

    async def fake_finish_aplay(process) -> None:
        return None

    audio._start_aplay = fake_start_aplay
    audio._write_playback = fake_write_playback
    audio._finish_aplay = fake_finish_aplay
    audio._terminate_process = lambda process: None

    loop_task = asyncio.create_task(audio._playback_loop())
    try:
        audio.begin_playback_stream()
        audio.enqueue_playback(b"first", sample_rate=24_000)
        await asyncio.sleep(0.005)
        audio.enqueue_playback(b"second", sample_rate=24_000)
        await audio._playback_queue.join()
        audio.finish_playback_stream()
        await asyncio.wait_for(ended.wait(), timeout=0.05)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert started_rates == [24_000]
    assert b"first" in written
    assert b"second" in written
    assert any(chunk and set(chunk) == {0} for chunk in written)
    assert audio.silence_fill_chunks > 0


@pytest.mark.asyncio
async def test_alsa_playback_loop_continues_after_batch_failure():
    ended = 0

    def on_end() -> None:
        nonlocal ended
        ended += 1

    audio = AlsaProcessAudioIO(
        SatelliteConfig(playback_end_settle_s=0.001),
        mic_queue=asyncio.Queue(),
        on_playback_end=on_end,
    )
    batches: list[tuple[bytes, int]] = []

    async def fake_play_stream(first_chunk: PlaybackChunk, *, first_from_queue: bool = False) -> None:
        batches.append((first_chunk.audio, first_chunk.sample_rate))
        if first_from_queue:
            audio._playback_queue.task_done()
        if len(batches) == 1:
            raise BrokenPipeError("aplay exited")

    audio._play_stream = fake_play_stream
    loop_task = asyncio.create_task(audio._playback_loop())
    try:
        audio.enqueue_playback(b"first", sample_rate=24_000)
        await asyncio.sleep(0)
        await audio._playback_queue.join()

        audio.enqueue_playback(b"second", sample_rate=24_000)
        await asyncio.sleep(0)
        await audio._playback_queue.join()
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert batches == [(b"first", 24_000), (b"second", 24_000)]
    assert ended == 2


@pytest.mark.asyncio
async def test_alsa_playback_uses_configured_channel_count(monkeypatch):
    created_command: list[str] = []

    async def fake_exec(*command, stdin=None, stderr=None):
        created_command.extend(command)
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    audio = AlsaProcessAudioIO(
        SatelliteConfig(playback_channels=2),
        mic_queue=asyncio.Queue(),
        on_playback_end=lambda: None,
    )

    await audio._start_aplay(24_000)

    assert created_command[created_command.index("-c") + 1] == "2"
