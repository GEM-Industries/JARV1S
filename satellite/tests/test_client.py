import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest

from jarvis_satellite.client import SatelliteClient, reconnect_delay
from jarvis_satellite.config import SatelliteConfig


class FakeAudio:
    def __init__(self, *, had_audio: bool = False) -> None:
        self.had_audio = had_audio
        self.stopped = False
        self.played: list[tuple[bytes, int]] = []
        self.stream_events: list[str] = []

    def start(self) -> None:
        return None

    def close(self) -> None:
        return None

    def enqueue_playback(self, audio: bytes, *, sample_rate: int) -> None:
        self.played.append((audio, sample_rate))

    def begin_playback_stream(self) -> None:
        self.stream_events.append("begin")

    def finish_playback_stream(self) -> None:
        self.stream_events.append("finish")

    def stop_playback(self) -> bool:
        self.stopped = True
        return self.had_audio


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.sent_event = asyncio.Event()

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))
        self.sent_event.set()


class FakeLed:
    def __init__(self) -> None:
        self.stages: list[str] = []

    def start(self) -> None:
        self.stages.append("start")

    async def stop(self) -> None:
        self.stages.append("stop")

    async def set_stage(self, stage: str) -> None:
        self.stages.append(stage)

    async def update_context(
        self,
        *,
        stage: str | None = None,
        soft_muted: bool | None = None,
        attention_mode: str | None = None,
    ) -> None:
        if stage is not None:
            self.stages.append(stage)
        if soft_muted is not None:
            self.stages.append(f"soft_muted={soft_muted}")
        if attention_mode is not None:
            self.stages.append(f"attention={attention_mode}")

    async def set_waking(self) -> None:
        self.stages.append("waking")

    async def set_connected(self) -> None:
        self.stages.append("off")

    async def set_disconnected(self) -> None:
        self.stages.append("disconnected")


def make_client(tmp_path: Path, audio: FakeAudio) -> SatelliteClient:
    client = SatelliteClient(
        SatelliteConfig(
            state_dir=tmp_path,
            node_id="jarvis-satellite-1",
            playback_end_settle_s=0.001,
            tts_end_timeout_s=0.01,
        )
    )
    client._audio = audio
    client._led = FakeLed()
    return client


def test_reconnect_delay_is_capped():
    assert reconnect_delay(1, base_delay_s=3, max_delay_s=30) == 3
    assert reconnect_delay(5, base_delay_s=3, max_delay_s=30) == 15
    assert reconnect_delay(20, base_delay_s=3, max_delay_s=30) == 30


def test_edge_wake_fails_closed_when_detector_cannot_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    client_module = importlib.import_module("jarvis_satellite.client")
    monkeypatch.setattr(client_module, "build_wake_detector", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="detector could not load"):
        SatelliteClient(
            SatelliteConfig(
                state_dir=tmp_path,
                node_id="jarvis-satellite-1",
                edge_wakeword=True,
            )
        )


def test_mint_ws_ticket_normalizes_raw_timeout(monkeypatch: pytest.MonkeyPatch):
    ticket_module = importlib.import_module("jarvis_satellite.ticket")

    def timeout(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr("jarvis_satellite.http.urlopen", timeout)

    with pytest.raises(RuntimeError, match="timed out"):
        ticket_module.mint_ws_ticket("ws://192.168.1.10:8000/api/v1/ws", "device-token")


@pytest.mark.asyncio
async def test_ticket_mint_failure_returns_to_reconnect_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client_module = sys.modules[SatelliteClient.__module__]

    def fail_mint(*args, **kwargs):
        raise RuntimeError("ws-ticket request failed: timed out")

    def fail_connect(*args, **kwargs):
        raise AssertionError("websocket connect should not run without a ticket")

    monkeypatch.setattr(client_module, "mint_ws_ticket", fail_mint)
    monkeypatch.setattr(client_module.websockets, "connect", fail_connect)

    client = SatelliteClient(
        SatelliteConfig(
            state_dir=tmp_path,
            node_id="jarvis-satellite-1",
            device_token="device-token",
        )
    )
    client._led = FakeLed()

    assert await client._run_once() is None
    assert client._led.stages == ["disconnected"]


@pytest.mark.asyncio
async def test_backend_stop_stops_playback_and_sends_playback_end_when_audio_was_active(tmp_path: Path):
    audio = FakeAudio(had_audio=True)
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    await client._handle_message(
        ws,
        {"type": "jarvis_audio", "data": {"audio": "AQI=", "sample_rate": 24_000, "turn_id": "turn-stop"}},
    )
    await client._handle_message(ws, {"type": "system.stop", "data": {}})

    assert audio.stopped is True
    assert [message["type"] for message in ws.sent] == ["audio.playback_end"]
    assert ws.sent[0]["data"] == {"turn_id": "turn-stop"}


@pytest.mark.asyncio
async def test_forced_stop_suppresses_follow_up_drain_playback_end(tmp_path: Path):
    audio = FakeAudio(had_audio=True)
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    loop_task = asyncio.create_task(client._playback_drained_loop(ws))
    try:
        await client._handle_message(ws, {"type": "system.stop", "data": {}})
        client._handle_playback_drained()
        await asyncio.sleep(0.01)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert [message["type"] for message in ws.sent] == ["audio.playback_end"]


@pytest.mark.asyncio
async def test_backend_stop_does_not_send_playback_end_when_no_audio_was_active(tmp_path: Path):
    audio = FakeAudio(had_audio=False)
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    await client._handle_message(ws, {"type": "system.stop", "data": {}})

    assert audio.stopped is True
    assert ws.sent == []


@pytest.mark.asyncio
async def test_barge_in_candidate_does_not_stop_playback(tmp_path: Path):
    audio = FakeAudio(had_audio=True)
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    await client._handle_message(ws, {"type": "speech.start", "data": {"barge_candidate": True}})

    assert audio.stopped is False
    assert ws.sent == []


@pytest.mark.asyncio
async def test_committed_speech_start_stops_local_audio(tmp_path: Path):
    audio = FakeAudio(had_audio=True)
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()
    stopped = False

    async def fake_stop() -> None:
        nonlocal stopped
        stopped = True

    client._notification_player.stop = fake_stop  # type: ignore[method-assign]

    await client._handle_message(ws, {"type": "speech.start", "data": {"is_speech": True}})

    assert stopped is True
    assert audio.stopped is True
    assert client._led.stages == ["listening"]
    assert [message["type"] for message in ws.sent] == ["audio.playback_end"]


@pytest.mark.asyncio
async def test_wake_word_speech_start_stops_local_audio(tmp_path: Path):
    audio = FakeAudio(had_audio=True)
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()
    stopped = False

    async def fake_stop() -> None:
        nonlocal stopped
        stopped = True

    client._notification_player.stop = fake_stop  # type: ignore[method-assign]

    await client._handle_message(ws, {"type": "speech.start", "data": {"wake_word": True}})

    assert stopped is True
    assert audio.stopped is True
    assert client._led.stages == ["waking"]
    assert [message["type"] for message in ws.sent] == ["audio.playback_end"]


@pytest.mark.asyncio
async def test_jarvis_audio_is_decoded_and_queued(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    await client._handle_message(
        ws,
        {"type": "jarvis_audio", "data": {"audio": "AQI=", "sample_rate": 24_000}},
    )

    assert audio.played == [(b"\x01\x02", 24_000)]
    assert audio.stream_events == ["begin"]


@pytest.mark.asyncio
async def test_playback_drain_waits_for_tts_end_marker(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    loop_task = asyncio.create_task(client._playback_drained_loop(ws))
    try:
        await client._handle_message(
            ws,
            {"type": "jarvis_audio", "data": {"audio": "AQI=", "sample_rate": 24_000, "turn_id": "turn-1"}},
        )
        client._handle_playback_drained()
        await asyncio.sleep(0)
        assert ws.sent == []

        await client._handle_message(ws, {"type": "audio.tts_end", "data": {"turn_id": "turn-1"}})
        await asyncio.wait_for(ws.sent_event.wait(), timeout=0.01)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert [message["type"] for message in ws.sent] == ["audio.playback_end"]
    assert ws.sent[0]["data"] == {"turn_id": "turn-1"}
    assert audio.stream_events == ["begin", "finish"]
    assert client._diagnostics.next_message() is None


@pytest.mark.asyncio
async def test_tts_end_marker_before_drain_sends_on_drain_without_tail(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    loop_task = asyncio.create_task(client._playback_drained_loop(ws))
    try:
        await client._handle_message(
            ws,
            {"type": "jarvis_audio", "data": {"audio": "AQI=", "sample_rate": 24_000, "turn_id": "turn-1"}},
        )
        await client._handle_message(ws, {"type": "audio.tts_end", "data": {"turn_id": "turn-1"}})
        client._handle_playback_drained()
        await asyncio.wait_for(ws.sent_event.wait(), timeout=0.01)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert [message["type"] for message in ws.sent] == ["audio.playback_end"]
    assert ws.sent[0]["data"] == {"turn_id": "turn-1"}


@pytest.mark.asyncio
async def test_missing_tts_end_marker_times_out_per_turn(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    loop_task = asyncio.create_task(client._playback_drained_loop(ws))
    try:
        await client._handle_message(
            ws,
            {"type": "jarvis_audio", "data": {"audio": "AQI=", "sample_rate": 24_000, "turn_id": "turn-1"}},
        )
        client._handle_playback_drained()
        await asyncio.wait_for(ws.sent_event.wait(), timeout=0.05)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert [message["type"] for message in ws.sent] == ["audio.playback_end"]
    assert ws.sent[0]["data"] == {"turn_id": "turn-1"}


@pytest.mark.asyncio
async def test_status_update_drives_led_stage(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    await client._handle_message(ws, {"type": "status.update", "data": {"stage": "speaking"}})

    assert client._led.stages == ["speaking"]


@pytest.mark.asyncio
async def test_status_update_drives_led_session_context(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    await client._handle_message(ws, {"type": "status.update", "data": {"stage": "idle", "session": {"soft_muted": True}}})

    assert client._led.stages == ["idle", "soft_muted=True"]


@pytest.mark.asyncio
async def test_system_connect_drives_led_attention_context(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    await client._handle_message(ws, {"type": "system.connect", "data": {"attention": {"mode": "paused"}}})

    assert client._led.stages == ["attention=paused"]


@pytest.mark.asyncio
async def test_wake_word_speech_start_drives_waking_led(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    await client._handle_message(ws, {"type": "speech.start", "data": {"wake_word": True}})

    assert client._led.stages == ["waking"]


@pytest.mark.asyncio
async def test_barge_candidate_speech_start_drives_listening_led(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    await client._handle_message(ws, {"type": "speech.start", "data": {"barge_candidate": True}})

    assert client._led.stages == ["listening"]


@pytest.mark.asyncio
async def test_notification_sound_plays_locally(tmp_path: Path):
    from jarvis_satellite.notification_audio import NotificationAudio
    import jarvis_satellite.notification_audio as notification_audio_module

    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()
    notification_audio_module.notification_audio.cache_clear()
    original_notification_audio = notification_audio_module.notification_audio
    notification_audio_module.notification_audio = lambda _sound: NotificationAudio(b"\x00\x00\x00\x00", 24_000)  # type: ignore[method-assign]

    try:
        await client._handle_message(ws, {"type": "notification.sound", "data": {"sound": "chime"}})
    finally:
        notification_audio_module.notification_audio = original_notification_audio  # type: ignore[method-assign]

    assert len(audio.played) == 1
    assert audio.played[0][0]
    assert audio.played[0][1] == 24_000


@pytest.mark.asyncio
async def test_audio_cue_plays_without_stopping_notification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from jarvis_satellite.notification_audio import NotificationAudio
    import jarvis_satellite.client as client_module

    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()
    stopped = False

    async def fake_stop() -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(client_module, "cue_audio", lambda _phase: NotificationAudio(b"\x00\x00\x00\x00", 24_000))
    client._notification_player.stop = fake_stop  # type: ignore[method-assign]

    await client._handle_message(ws, {"type": "audio.cue", "data": {"phase": "start"}})

    assert stopped is False
    assert audio.played == [(b"\x00\x00\x00\x00", 24_000)]
    assert client._suppress_playback_drain_generation == client._playback_generation


@pytest.mark.asyncio
async def test_audio_cue_respects_disabled_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from jarvis_satellite.notification_audio import NotificationAudio
    import jarvis_satellite.client as client_module

    audio = FakeAudio()
    client = SatelliteClient(
        SatelliteConfig(
            state_dir=tmp_path,
            node_id="jarvis-satellite-1",
            tool_cues_enabled=False,
        )
    )
    client._audio = audio
    client._led = FakeLed()
    ws = FakeWebSocket()
    monkeypatch.setattr(client_module, "cue_audio", lambda _phase: NotificationAudio(b"\x00\x00\x00\x00", 24_000))

    await client._handle_message(ws, {"type": "audio.cue", "data": {"phase": "start"}})

    assert audio.played == []


@pytest.mark.asyncio
async def test_audio_cue_respects_owner_preferences_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from jarvis_satellite.notification_audio import NotificationAudio
    import jarvis_satellite.client as client_module

    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()
    monkeypatch.setattr(client_module, "cue_audio", lambda _phase: NotificationAudio(b"\x00\x00\x00\x00", 24_000))

    await client._handle_message(
        ws,
        {
            "type": "preferences.update",
            "data": {"preferences": {"audio": {"tool_cues_enabled": False}}},
        },
    )
    await client._handle_message(ws, {"type": "audio.cue", "data": {"phase": "start"}})

    assert audio.played == []


@pytest.mark.asyncio
async def test_audio_cue_reads_preferences_from_connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from jarvis_satellite.notification_audio import NotificationAudio
    import jarvis_satellite.client as client_module

    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()
    monkeypatch.setattr(client_module, "cue_audio", lambda _phase: NotificationAudio(b"\x00\x00\x00\x00", 24_000))

    await client._handle_message(
        ws,
        {
            "type": "system.connect",
            "data": {"preferences": {"audio": {"tool_cues_enabled": False}}},
        },
    )
    await client._handle_message(ws, {"type": "audio.cue", "data": {"phase": "done"}})

    assert audio.played == []


@pytest.mark.asyncio
async def test_system_stop_stops_notification_sound(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()
    stopped = False

    async def fake_stop() -> None:
        nonlocal stopped
        stopped = True

    client._notification_player.stop = fake_stop  # type: ignore[method-assign]

    await client._handle_message(ws, {"type": "system.stop", "data": {}})

    assert stopped is True


@pytest.mark.asyncio
async def test_tts_ducks_notification_until_playback_end(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()
    events: list[str] = []

    client._notification_player.duck = lambda: events.append("duck")  # type: ignore[method-assign]
    client._notification_player.unduck = lambda: events.append("unduck")  # type: ignore[method-assign]

    await client._handle_message(
        ws,
        {
            "type": "jarvis_audio",
            "data": {"audio": "AAAAAA==", "sample_rate": 24_000},
        },
    )
    await client._send_playback_end_once(ws, client._playback_generation, reason="test")

    assert events == ["duck", "unduck"]


@pytest.mark.asyncio
async def test_empty_drain_without_audio_does_not_send_playback_end(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    ws = FakeWebSocket()

    loop_task = asyncio.create_task(client._playback_drained_loop(ws))
    try:
        client._handle_playback_drained()
        await asyncio.sleep(0.02)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    assert ws.sent == []


def test_audio_health_emits_only_new_failure_counters(tmp_path: Path):
    audio = FakeAudio()
    audio.input_overflows = 1
    audio.capture_restarts = 1
    audio.playback_dropped_chunks = 2
    audio.playback_failures = 1
    client = make_client(tmp_path, audio)

    client._poll_audio_health()
    first = client._diagnostics.drain_messages()
    assert [event["event"] for event in first[0]["data"]["events"]] == [
        "mic_interrupted",
        "mic_interrupted",
        "playback_failed",
    ]

    client._poll_audio_health()
    assert client._diagnostics.next_message() is None


class FakeWake:
    def __init__(self, hit_on: int = 2) -> None:
        self.hit_on = hit_on
        self.calls = 0
        self.resets = 0

    @property
    def model_loaded(self) -> bool:
        return True

    def reset(self) -> None:
        self.resets += 1
        self.calls = 0

    def process(self, chunk: bytes):
        from jarvis_satellite.wakeword import LocalWakeHit

        self.calls += 1
        if self.calls == self.hit_on:
            return LocalWakeHit(score=0.91)
        return None


@pytest.mark.asyncio
async def test_edge_wake_holds_idle_pcm_then_flushes_preroll(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    client._edge_wake_enabled = True
    client._wake = FakeWake(hit_on=3)
    client._preroll_max_bytes = 10_000
    ws = FakeWebSocket()

    async def feed():
        for payload in (b"aa", b"bb", b"cc", b"dd"):
            await client._mic_queue.put(payload)
        await asyncio.sleep(0.05)

    loop_task = asyncio.create_task(client._mic_loop(ws))
    feed_task = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(feed_task, timeout=1.0)
        await asyncio.sleep(0.05)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    types = [message["type"] for message in ws.sent]
    assert types[0] == "voice.activate"
    assert types[1:] == ["user_audio", "user_audio", "user_audio", "user_audio"]
    assert client._streaming_to_host is True
    assert client._led.stages == ["waking"]


@pytest.mark.asyncio
async def test_edge_wake_returns_local_only_on_idle_status(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    client._edge_wake_enabled = True
    wake = FakeWake(hit_on=99)
    client._wake = wake
    client._streaming_to_host = True
    client._note_preroll(b"stale")

    await client._handle_message(FakeWebSocket(), {"type": "status.update", "data": {"stage": "idle"}})

    assert client._streaming_to_host is False
    assert list(client._preroll) == []
    assert wake.resets == 1
    assert client._should_stream_to_host() is False


@pytest.mark.asyncio
async def test_edge_wake_streams_during_proactive_speaking(tmp_path: Path):
    audio = FakeAudio()
    client = make_client(tmp_path, audio)
    client._edge_wake_enabled = True
    client._wake = FakeWake(hit_on=99)
    assert client._should_stream_to_host() is False

    await client._handle_message(FakeWebSocket(), {"type": "status.update", "data": {"stage": "speaking"}})
    assert client._should_stream_to_host() is True
