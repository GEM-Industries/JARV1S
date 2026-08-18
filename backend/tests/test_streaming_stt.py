import asyncio
import json
import time
from unittest.mock import patch

from core import settings

from core.voice.stt_service import (
    AppleSpeechEvent,
    AppleSpeechSTTSession,
    CartesiaStreamingSTTSession,
    _parse_apple_stt_event,
)
from core.voice.streaming_stt import StreamingSTTCoordinator


def test_cartesia_session_stats_expose_protocol():
    session = CartesiaStreamingSTTSession(
        api_key="test",
        language="en",
        sample_rate=16000,
    )
    assert session.stats["protocol"] == "cartesia"


def test_cartesia_finish_returns_before_background_close_completes():
    async def run() -> None:
        session = CartesiaStreamingSTTSession(
            api_key="test",
            language="en",
            sample_rate=16000,
        )
        close_started = asyncio.Event()
        close_done = asyncio.Event()
        original_close = CartesiaStreamingSTTSession.close

        async def slow_close(self) -> None:
            close_started.set()
            await asyncio.sleep(0.15)
            await original_close(self)
            close_done.set()

        class MockWs:
            async def send(self, msg: str) -> None:
                if msg == "finalize":
                    session._done_event.set()

            async def close(self) -> None:
                return None

        session._ws = MockWs()
        session._append_final("hello world")

        with patch.object(CartesiaStreamingSTTSession, "close", slow_close):
            started = time.monotonic()
            transcript = await session.finish(timeout_s=1.0)
            finish_ms = (time.monotonic() - started) * 1000
            assert not close_started.is_set()
            assert transcript == "hello world"
            assert finish_ms < 50
            await asyncio.wait_for(close_started.wait(), timeout=1.0)
            await asyncio.wait_for(close_done.wait(), timeout=1.0)

    asyncio.run(run())


def test_cartesia_streaming_stt_appends_final_deltas_verbatim():
    session = CartesiaStreamingSTTSession(
        api_key="test",
        language="en",
        sample_rate=16000,
    )

    session._append_final("Insert")
    session._latest_interim = "ing spaces is not safe"
    assert session._assembled_text(include_interim=True) == "Inserting spaces is not safe"

    session._latest_interim = ""
    session._append_final("ing spaces is not safe")
    assert session._assembled_text(include_interim=True) == "Inserting spaces is not safe"
    assert session._assembled_text(include_interim=False) == "Inserting spaces is not safe"


def test_streaming_stt_coordinator_emits_changed_partials(monkeypatch):
    monkeypatch.setattr(settings.VOICE, "stt_partial_emit_interval_s", 0)
    partials: list[str] = []

    async def run() -> None:
        class FakeStream:
            async def feed(self, audio_bytes: bytes) -> None:
                await callback("hello", False)
                await callback("hello", False)
                await callback("hello world", False)

            async def finish(self, *, timeout_s: float) -> str:
                await callback("hello world", True)
                return "hello world"

            async def close(self) -> None:
                return None

        callback = None

        class FakeBackend:
            async def start_streaming(self, on_transcript=None, on_turn_end=None):
                del on_turn_end
                nonlocal callback
                callback = on_transcript
                return FakeStream()

        async def on_partial(text: str) -> None:
            partials.append(text)

        coordinator = StreamingSTTCoordinator(
            stt=FakeBackend(),
            session_id="test",
            on_partial=on_partial,
        )

        assert coordinator.stream_id.startswith("stt-")
        assert len(coordinator.stream_id) == len("stt-") + 12
        assert await coordinator.start()
        await coordinator.feed(b"audio")
        assert coordinator.latest_text == "hello world"
        assert coordinator.latest_text_updated_at > 0
        assert coordinator.latest_text_is_final is False
        assert await coordinator.finish() == "hello world"
        assert coordinator.latest_text_is_final is True

    asyncio.run(run())
    assert partials == ["hello", "hello world", "hello world"]


def test_streaming_stt_coordinator_splits_initial_audio():
    chunks: list[bytes] = []
    initial_audio = b"a" * 61440

    async def run() -> None:
        class FakeStream:
            async def feed(self, audio_bytes: bytes) -> None:
                chunks.append(audio_bytes)

            async def finish(self, *, timeout_s: float) -> str:
                return "hello"

            async def close(self) -> None:
                return None

        class FakeBackend:
            async def start_streaming(self, on_transcript=None, on_turn_end=None):
                del on_transcript, on_turn_end
                return FakeStream()

        coordinator = StreamingSTTCoordinator(
            stt=FakeBackend(),
            session_id="test",
        )

        assert await coordinator.start(initial_audio=initial_audio)

    asyncio.run(run())
    assert b"".join(chunks) == initial_audio
    assert len(chunks) > 1
    assert max(len(chunk) for chunk in chunks) == 3072


def test_streaming_stt_coordinator_does_not_return_shorter_than_latest_partial():
    async def run() -> str:
        class FakeStream:
            async def feed(self, audio_bytes: bytes) -> None:
                return None

            async def finish(self, *, timeout_s: float) -> str:
                await callback("Well, just let me know if you do.", True)
                await callback("Well, just let me know if you do. and I will consider it.", True)
                return "Well, just let me know if you do."

            async def close(self) -> None:
                return None

        callback = None

        class FakeBackend:
            async def start_streaming(self, on_transcript=None, on_turn_end=None):
                del on_turn_end
                nonlocal callback
                callback = on_transcript
                return FakeStream()

        coordinator = StreamingSTTCoordinator(stt=FakeBackend(), session_id="test")
        assert await coordinator.start()
        return await coordinator.finish()

    transcript = asyncio.run(run())
    assert transcript == "Well, just let me know if you do. and I will consider it."


def test_apple_stt_event_partial_and_final():
    partial = _parse_apple_stt_event({"type": "partial", "text": "hello"})
    final = _parse_apple_stt_event({"type": "final", "text": "hello world"})
    done = _parse_apple_stt_event({"type": "done"})

    assert partial.text == "hello"
    assert partial.is_final is False
    assert partial.is_terminal is False
    assert final.text == "hello world"
    assert final.is_final is True
    assert final.is_terminal is False
    assert done.is_terminal is True


def test_apple_speech_session_replaces_cumulative_transcripts():
    seen: list[tuple[str, bool]] = []

    async def run() -> str:
        async def on_transcript(text: str, is_final: bool) -> None:
            seen.append((text, is_final))

        session = AppleSpeechSTTSession(
            url="ws://local.test/asr",
            sample_rate=16000,
            on_transcript=on_transcript,
        )
        await session._handle_event(AppleSpeechEvent(text="hello", is_final=False))
        await session._handle_event(AppleSpeechEvent(text="hello world", is_final=False))
        return await session.finish(timeout_s=0.01)

    transcript = asyncio.run(run())
    assert transcript == "hello world"
    assert seen == [("hello", False), ("hello world", False)]


def test_apple_session_uses_json_finalize():
    sent: list[str] = []

    class MockWs:
        async def send(self, message: str) -> None:
            sent.append(message)

        async def close(self) -> None:
            return None

    async def run() -> str:
        session = AppleSpeechSTTSession(
            url="ws://local.test/asr",
            sample_rate=16000,
        )
        session._ws = MockWs()
        session._latest_text = "hello world"
        session._done_event.set()
        await session._send_start_message()
        return await session.finish(timeout_s=0.01)

    assert asyncio.run(run()) == "hello world"
    assert [json.loads(message) for message in sent] == [
        {
            "type": "start",
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
        },
        {"type": "finalize"},
        {"type": "cancel"},
    ]


def test_apple_final_prefix_does_not_end_stream():
    async def run() -> tuple[str, bool]:
        session = AppleSpeechSTTSession(
            url="ws://local.test/asr",
            sample_rate=16000,
        )
        await session._handle_event(AppleSpeechEvent(text="hello", is_final=True))
        done_after_final = session._done_event.is_set()
        await session._handle_event(
            AppleSpeechEvent(text="hello world", is_final=True, is_terminal=True)
        )
        return await session.finish(timeout_s=0.01), done_after_final

    transcript, done_after_final = asyncio.run(run())
    assert done_after_final is False
    assert transcript == "hello world"


def test_streaming_stt_coordinator_handles_provider_turn_end():
    turn_ends: list[str] = []

    async def run() -> None:
        class FakeStream:
            async def feed(self, audio_bytes: bytes) -> None:
                await turn_end_callback("hello there")

            async def finish(self, *, timeout_s: float) -> str:
                return "hello there"

            async def close(self) -> None:
                return None

        turn_end_callback = None

        class FakeBackend:
            async def start_streaming(self, on_transcript=None, on_turn_end=None):
                nonlocal turn_end_callback
                turn_end_callback = on_turn_end
                return FakeStream()

        async def on_provider_turn_end(text: str) -> None:
            turn_ends.append(text)

        coordinator = StreamingSTTCoordinator(
            stt=FakeBackend(),
            session_id="test",
            on_provider_turn_end=on_provider_turn_end,
        )
        assert await coordinator.start()
        await coordinator.feed(b"audio")

    asyncio.run(run())
    assert turn_ends == ["hello there"]
