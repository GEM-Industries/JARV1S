import asyncio
import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from api.websockets import handlers
from api.websockets.connection import VoiceInputTurn
from api.websockets.models import WSMessage
from api.websockets.types import WSMessageType
from core.voice import turn_detector as turn_detector_mod
from core.voice.turn_detector import AudioTurnDetectorSession, TurnDecision
from services.events import EventType


def test_audio_turn_detector_lazy_stream_and_threshold(monkeypatch):
    async def run() -> None:
        push_audio = MagicMock()
        flush = MagicMock()
        stream = SimpleNamespace(
            push_audio=push_audio,
            flush=flush,
            prediction_timeout=1.0,
            cancel_inference=MagicMock(),
            predict=AsyncMock(
                return_value=SimpleNamespace(end_of_turn_probability=0.9),
            ),
            unlikely_threshold=AsyncMock(return_value=0.5),
            aclose=AsyncMock(),
        )
        detector = SimpleNamespace(stream=MagicMock(return_value=stream))
        monkeypatch.setattr(turn_detector_mod, "_detector", None)
        monkeypatch.setattr(turn_detector_mod, "_get_detector", lambda: detector)

        session = AudioTurnDetectorSession(sample_rate=16000, channels=1)
        assert detector.stream.call_count == 0

        pcm = b"\x00\x01" * 160
        session.push_pcm(pcm)
        assert detector.stream.call_count == 1
        push_audio.assert_called_once()
        frame = push_audio.call_args.args[0]
        assert frame.sample_rate == 16000
        assert frame.num_channels == 1
        assert frame.samples_per_channel == 160

        decision = await session.predict(language="en")
        assert decision.done is True
        assert decision.confidence == 0.9
        assert decision.reason == "audio_eou"

        session.flush()
        flush.assert_called_once_with()
        await session.aclose()
        stream.aclose.assert_awaited_once()
        assert session._stream is None

    asyncio.run(run())


def test_audio_turn_detector_chunks_large_pcm_batches(monkeypatch):
    push_audio = MagicMock()
    stream = SimpleNamespace(push_audio=push_audio)
    monkeypatch.setattr(
        turn_detector_mod,
        "_get_detector",
        lambda: SimpleNamespace(stream=MagicMock(return_value=stream)),
    )

    session = AudioTurnDetectorSession(sample_rate=16000, channels=1)
    session.push_pcm(b"\x00\x01" * (1600 * 3 + 80))

    assert push_audio.call_count == 4
    frames = [call.args[0] for call in push_audio.call_args_list]
    assert [frame.samples_per_channel for frame in frames] == [1600, 1600, 1600, 80]


def test_audio_turn_detector_continue_below_threshold(monkeypatch):
    async def run() -> None:
        stream = SimpleNamespace(
            push_audio=MagicMock(),
            flush=MagicMock(),
            prediction_timeout=1.0,
            cancel_inference=MagicMock(),
            predict=AsyncMock(
                return_value=SimpleNamespace(end_of_turn_probability=0.2),
            ),
            unlikely_threshold=AsyncMock(return_value=0.5),
            aclose=AsyncMock(),
        )
        monkeypatch.setattr(
            turn_detector_mod,
            "_get_detector",
            lambda: SimpleNamespace(stream=MagicMock(return_value=stream)),
        )
        session = AudioTurnDetectorSession()
        decision = await session.predict(language="en")
        assert decision.done is False
        assert decision.reason == "audio_eou"

    asyncio.run(run())


def test_audio_turn_detector_vad_fallback_on_prediction_timeout(monkeypatch):
    async def run() -> None:
        prediction: asyncio.Future = asyncio.get_running_loop().create_future()
        stream = SimpleNamespace(
            push_audio=MagicMock(),
            prediction_timeout=0.001,
            cancel_inference=MagicMock(side_effect=lambda **_: prediction.set_result(None)),
            predict=MagicMock(return_value=prediction),
            aclose=AsyncMock(),
        )
        monkeypatch.setattr(
            turn_detector_mod,
            "_get_detector",
            lambda: SimpleNamespace(stream=MagicMock(return_value=stream)),
        )

        decision = await AudioTurnDetectorSession().predict(language="en")

        assert decision.done is True
        assert decision.reason == "vad_fallback"
        stream.cancel_inference.assert_called_once_with(timed_out=True)

    asyncio.run(run())


def test_audio_turn_detector_vad_fallback_on_failure(monkeypatch):
    async def run() -> None:
        monkeypatch.setattr(
            turn_detector_mod,
            "_get_detector",
            lambda: (_ for _ in ()).throw(RuntimeError("model unavailable")),
        )
        session = AudioTurnDetectorSession()
        session.push_pcm(b"\x00\x01" * 80)
        decision = await session.predict(language="en")
        assert decision.done is True
        assert decision.reason == "vad_fallback"

    asyncio.run(run())


def test_handler_turn_detector_feed_flush_and_close(monkeypatch):
    async def run() -> None:
        detector = SimpleNamespace(
            push_pcm=MagicMock(),
            flush=MagicMock(),
            aclose=AsyncMock(),
            predict=AsyncMock(return_value=TurnDecision(done=True, reason="audio_eou")),
        )
        session = SimpleNamespace(turn_detector=None)

        def ensure(s):
            s.turn_detector = detector
            return detector

        monkeypatch.setattr(handlers, "_ensure_turn_detector", ensure)

        handlers._feed_turn_detector(session, b"\x00\x01" * 16)
        detector.push_pcm.assert_called_once()
        handlers._flush_turn_detector(session)
        detector.flush.assert_called_once_with()
        await handlers._close_turn_detector(session)
        detector.aclose.assert_awaited_once()
        assert session.turn_detector is None

    asyncio.run(run())


def test_push_to_talk_commit_submits_captured_turn(monkeypatch):
    async def run() -> None:
        processor = SimpleNamespace(request_turn_commit=MagicMock(return_value=True))
        voice_turn = VoiceInputTurn(turn_id="turn-ptt", transcript_text="Turn on the lights")
        session = SimpleNamespace(
            processor=processor,
            voice_turn=voice_turn,
            stt_stream=None,
            endpoint_decision_task=None,
            accepted_input_task=None,
            barge_in_candidate_started_at=0.0,
            barge_in_candidate_committed=False,
        )
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        commit = AsyncMock()
        publish = AsyncMock()
        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "_commit_voice_turn", commit)
        monkeypatch.setattr(handlers, "event_bus", SimpleNamespace(publish=publish))

        await handlers.handle_voice_commit(
            "test",
            WSMessage(type=WSMessageType.VOICE_COMMIT, data={}),
        )

        processor.request_turn_commit.assert_called_once_with()
        publish.assert_awaited_once()
        commit.assert_awaited_once()
        decision = commit.await_args.args[3]
        assert decision.done is True
        assert decision.reason == "push_to_talk_release"
        assert any(
            call.args[1].data == {"stage": "transcribing"}
            for call in fake_manager.send_message.await_args_list
        )

    asyncio.run(run())


def test_turn_detector_continue_preserves_voice_turn(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=False, confidence=0.1, reason="fake_continue")

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_max_delay", 10.0)

        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-1",
                transcript_text="I want to",
                endpoint_candidate_started_at=time.monotonic(),
            ),
            stt_stream=SimpleNamespace(latest_text="I want to"),
        )

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)
        assert should_commit is False
        assert decision.reason == "fake_continue"
        assert session.voice_turn.transcript_text == "I want to"

    asyncio.run(run())


def _apple_speech_config():
    return SimpleNamespace(stt_provider="apple_speech")


def test_apple_speech_waits_for_recent_partial_to_stabilize(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                raise AssertionError("semantic EOU should wait for stable streaming text")

        now = time.monotonic()
        text = "The planning docs are ready for another round"
        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-1",
                transcript_text=text,
                endpoint_candidate_started_at=now - 0.2,
                endpoint_candidate_text_chars=len(text),
            ),
            stt_stream=SimpleNamespace(
                latest_text=text,
                latest_text_updated_at=now,
            ),
        )

        monkeypatch.setattr(handlers, "resolve_voice_config_sync", _apple_speech_config)
        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "apple_speech_commit_stability_delay", 0.25)

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)

        assert should_commit is False
        assert decision.reason == "streaming_transcript_unstable"

    asyncio.run(run())


def test_apple_speech_does_not_delay_provider_final_transcript(monkeypatch):
    now = time.monotonic()
    session = SimpleNamespace(
        stt_stream=SimpleNamespace(
            latest_text="Thanks.",
            latest_text_updated_at=now,
            latest_text_is_final=True,
        ),
    )

    monkeypatch.setattr(handlers, "resolve_voice_config_sync", _apple_speech_config)
    monkeypatch.setattr(handlers.settings.VOICE, "apple_speech_commit_stability_delay", 0.1)

    stable, age = handlers._apple_speech_transcript_stability(session, now)

    assert stable is True
    assert age is None


def test_apple_speech_waits_briefly_on_semantic_continue(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=False, confidence=0.2, reason="fake_continue")

        now = time.monotonic()
        text = "Can you check my calendar tomorrow?"
        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-1",
                transcript_text=text,
                endpoint_candidate_started_at=now - 0.2,
                endpoint_candidate_text_chars=len(text),
            ),
            stt_stream=SimpleNamespace(
                latest_text=text,
                latest_text_updated_at=now - 1.0,
            ),
        )

        monkeypatch.setattr(handlers, "resolve_voice_config_sync", _apple_speech_config)
        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "apple_speech_commit_stability_delay", 0.2)
        monkeypatch.setattr(handlers.settings.VOICE, "apple_speech_endpoint_max_delay", 0.45)

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)

        assert should_commit is False
        assert decision.reason == "local_endpoint_wait:fake_continue"

    asyncio.run(run())


def test_apple_speech_max_delay_commits_after_semantic_continue(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=False, confidence=0.2, reason="fake_continue")

        now = time.monotonic()
        text = "Can you check my calendar tomorrow?"
        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-1",
                transcript_text=text,
                endpoint_candidate_started_at=now - 1.0,
                endpoint_candidate_text_chars=len(text),
            ),
            stt_stream=SimpleNamespace(
                latest_text=text,
                latest_text_updated_at=now - 1.0,
            ),
        )

        monkeypatch.setattr(handlers, "resolve_voice_config_sync", _apple_speech_config)
        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "apple_speech_commit_stability_delay", 0.2)
        monkeypatch.setattr(handlers.settings.VOICE, "apple_speech_endpoint_max_delay", 0.45)

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)

        assert should_commit is True
        assert decision.reason == "local_endpoint_max_delay:fake_continue"

    asyncio.run(run())


def test_apple_speech_holds_mid_thought_inside_endpoint_max_delay(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=False, confidence=0.057, reason="audio_eou")

        now = time.monotonic()
        text = "And also, it's not even the fact that it's like,"
        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-hold",
                transcript_text=text,
                endpoint_candidate_started_at=now - 0.27,
                endpoint_candidate_text_chars=len(text),
            ),
            stt_stream=SimpleNamespace(
                latest_text=text,
                latest_text_updated_at=now - 1.0,
            ),
        )

        monkeypatch.setattr(handlers, "resolve_voice_config_sync", _apple_speech_config)
        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "apple_speech_endpoint_max_delay", 2.5)

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)

        assert should_commit is False
        assert decision.reason == "local_endpoint_wait:audio_eou"

    asyncio.run(run())


def test_apple_speech_short_done_utterance_ignores_endpoint_max_delay(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=True, confidence=0.69, reason="audio_eou")

        now = time.monotonic()
        text = "Yes please."
        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-short",
                transcript_text=text,
                endpoint_candidate_started_at=now,
                endpoint_candidate_text_chars=len(text),
            ),
            stt_stream=SimpleNamespace(
                latest_text=text,
                latest_text_updated_at=now - 0.2,
                latest_text_is_final=True,
            ),
        )

        monkeypatch.setattr(handlers, "resolve_voice_config_sync", _apple_speech_config)
        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "apple_speech_endpoint_max_delay", 2.5)

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)

        assert should_commit is True
        assert decision.reason == "audio_eou"

    asyncio.run(run())


def test_apply_voice_turn_transcript_rejects_shorter_candidate():
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="I want to keep talking.",
    )

    accepted = handlers._apply_voice_turn_transcript(
        "test",
        voice_turn,
        "I want to",
        event="test_transcript_candidate",
        reason="test",
    )

    assert accepted is False
    assert voice_turn.transcript_text == "I want to keep talking."


def test_apply_voice_turn_transcript_accepts_longer_candidate():
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="I want to",
    )

    accepted = handlers._apply_voice_turn_transcript(
        "test",
        voice_turn,
        "I want to keep talking.",
        event="test_transcript_candidate",
        reason="test",
    )

    assert accepted is True
    assert voice_turn.transcript_text == "I want to keep talking."


def test_sync_voice_turn_transcript_merges_continuation_prefix():
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="Hey Jarvis would you",
        continuation_prefix="Hey Jarvis would you",
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        stt_stream=SimpleNamespace(latest_text="you be able to check this"),
    )

    text = handlers._sync_voice_turn_transcript("test", session, reason="test")

    assert text == "Hey Jarvis would you be able to check this"
    assert voice_turn.transcript_text == text


def test_fast_recovery_does_not_prepend_prior_audio(monkeypatch):
    async def run() -> None:
        class FakeProcessor:
            mode = handlers.VoiceMode.ACTIVE_AI_TURN
            turn_phase = handlers.SpeechTurnPhase.SPEAKING

            def __init__(self) -> None:
                self.turn_buffer = bytearray(b"new-audio")

            async def add_audio(
                self,
                audio_bytes: bytes,
                *,
                retain_preroll: bool = True,
                preroll_seconds: float | None = None,
            ):
                return handlers.SpeechEvent.USER_TURN_STARTED

        class FakeTask:
            def __init__(self) -> None:
                self.cancelled_with = None

            def done(self) -> bool:
                return False

            def cancel(self, reason=None) -> None:
                self.cancelled_with = reason

        processor = FakeProcessor()
        task = FakeTask()
        current_delivery = SimpleNamespace(response_id="response-1", signal_cancel=MagicMock())
        voice_turn = VoiceInputTurn(
            turn_id="turn-1",
            last_endpoint_monotonic=time.monotonic(),
            transcript_text="Hey Jarvis would you",
        )
        session = SimpleNamespace(
            processor=processor,
            voice_turn=voice_turn,
            accepted_input_task=None,
            current_run_task=task,
            endpoint_decision_task=None,
            current_delivery=current_delivery,
            stt_stream=None,
        )
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        start_streaming = AsyncMock()
        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "_start_streaming_stt", start_streaming)

        message = WSMessage(
            type=WSMessageType.USER_AUDIO,
            data={
                "audio": base64.b64encode(b"chunk").decode("ascii"),
                "encoding": "base64",
            },
        )

        await handlers.handle_audio_stream("test", message)

        assert voice_turn.continuation_prefix == "Hey Jarvis would you"
        current_delivery.signal_cancel.assert_called_once_with()
        assert task.cancelled_with == "fast_recovery"
        start_streaming.assert_awaited_once_with("test", session, b"new-audio")
        retract = next(
            call.args[1]
            for call in fake_manager.send_message.await_args_list
            if call.args[1].type == WSMessageType.RETRACT
        )
        assert retract.data == {
            "response_id": "response-1",
            "turn_id": "turn-1",
        }

    asyncio.run(run())


def test_fast_recovery_waits_for_inflight_input_commit_with_existing_transcript(monkeypatch):
    async def run() -> None:
        class FakeProcessor:
            mode = handlers.VoiceMode.ACTIVE_AI_TURN
            turn_phase = handlers.SpeechTurnPhase.SPEAKING

            def __init__(self) -> None:
                self.turn_buffer = bytearray(b"new-audio")

            async def add_audio(
                self,
                audio_bytes: bytes,
                *,
                retain_preroll: bool = True,
                preroll_seconds: float | None = None,
            ):
                return handlers.SpeechEvent.USER_TURN_STARTED

        class AwaitableInputTask:
            def __init__(self, voice_turn: VoiceInputTurn) -> None:
                self.voice_turn = voice_turn
                self.awaited = False
                self.cancelled_with = None

            def done(self) -> bool:
                return False

            def cancel(self, reason=None) -> None:
                self.cancelled_with = reason

            def __await__(self):
                async def settle():
                    self.awaited = True
                    self.voice_turn.transcript_text = "Yeah, but why is it called creepypasta"

                return settle().__await__()

        class FakeRunTask:
            def __init__(self) -> None:
                self.cancelled_with = None

            def done(self) -> bool:
                return False

            def cancel(self, reason=None) -> None:
                self.cancelled_with = reason

        processor = FakeProcessor()
        current_delivery = SimpleNamespace(response_id="response-1", signal_cancel=MagicMock())
        voice_turn = VoiceInputTurn(
            turn_id="turn-1",
            last_endpoint_monotonic=time.monotonic(),
            transcript_text="Yeah, but why is it called",
        )
        input_task = AwaitableInputTask(voice_turn)
        run_task = FakeRunTask()
        session = SimpleNamespace(
            processor=processor,
            voice_turn=voice_turn,
            accepted_input_task=input_task,
            current_run_task=run_task,
            endpoint_decision_task=None,
            current_delivery=current_delivery,
            stt_stream=None,
        )
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
            send_voice_response=AsyncMock(),
        )
        start_streaming = AsyncMock()
        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "_start_streaming_stt", start_streaming)

        message = WSMessage(
            type=WSMessageType.USER_AUDIO,
            data={
                "audio": base64.b64encode(b"chunk").decode("ascii"),
                "encoding": "base64",
            },
        )

        await handlers.handle_audio_stream("test", message)

        assert input_task.awaited is True
        assert input_task.cancelled_with is None
        assert run_task.cancelled_with == "fast_recovery"
        assert voice_turn.continuation_prefix == "Yeah, but why is it called creepypasta"
        fake_manager.send_voice_response.assert_awaited_once()
        start_streaming.assert_awaited_once_with("test", session, b"new-audio")

    asyncio.run(run())


def test_fast_recovery_miss_reason_only_for_in_window_speech():
    window = 2.0
    running = SimpleNamespace(done=lambda: False)

    assert (
        handlers._fast_recovery_miss_reason(
            SimpleNamespace(voice_turn=None, accepted_input_task=None, current_run_task=None),
            elapsed=float("inf"),
            window=window,
        )
        is None
    )
    assert (
        handlers._fast_recovery_miss_reason(
            SimpleNamespace(voice_turn=None, accepted_input_task=None, current_run_task=None),
            elapsed=0.4,
            window=window,
        )
        == "no_voice_turn"
    )
    assert (
        handlers._fast_recovery_miss_reason(
            SimpleNamespace(
                voice_turn=VoiceInputTurn(turn_id="turn-1"),
                accepted_input_task=None,
                current_run_task=None,
            ),
            elapsed=0.4,
            window=window,
        )
        == "no_task"
    )
    assert (
        handlers._fast_recovery_miss_reason(
            SimpleNamespace(
                voice_turn=VoiceInputTurn(turn_id="turn-1"),
                accepted_input_task=running,
                current_run_task=None,
            ),
            elapsed=0.4,
            window=window,
        )
        == "window_ok_but_ineligible"
    )


def test_non_recovery_speech_starts_fresh_voice_turn(monkeypatch):
    async def run() -> None:
        class FakeProcessor:
            mode = handlers.VoiceMode.ACTIVE_AI_TURN
            turn_phase = handlers.SpeechTurnPhase.SPEAKING

            def __init__(self) -> None:
                self.turn_buffer = bytearray(b"new-turn-audio")

            async def add_audio(
                self,
                audio_bytes: bytes,
                *,
                retain_preroll: bool = True,
                preroll_seconds: float | None = None,
            ):
                return handlers.SpeechEvent.USER_TURN_STARTED

        class FakeTask:
            def done(self) -> bool:
                return False

        old_voice_turn = VoiceInputTurn(
            turn_id="old-turn",
            last_endpoint_monotonic=time.monotonic() - 5.0,
            transcript_text="old transcript",
        )
        session = SimpleNamespace(
            processor=FakeProcessor(),
            voice_turn=old_voice_turn,
            accepted_input_task=None,
            current_run_task=FakeTask(),
            endpoint_decision_task=None,
            first_audio_sent=True,
        )
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        fake_bus = SimpleNamespace(publish=AsyncMock())
        start_streaming = AsyncMock()
        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "event_bus", fake_bus)
        monkeypatch.setattr(handlers, "_start_streaming_stt", start_streaming)

        message = WSMessage(
            type=WSMessageType.USER_AUDIO,
            data={
                "audio": base64.b64encode(b"chunk").decode("ascii"),
                "encoding": "base64",
            },
        )

        await handlers.handle_audio_stream("test", message)

        assert session.voice_turn is not old_voice_turn
        assert session.voice_turn.turn_id != "old-turn"
        assert session.voice_turn.transcript_text == ""
        fake_bus.publish.assert_awaited_once()
        start_streaming.assert_awaited_once_with("test", session, b"new-turn-audio")

    asyncio.run(run())


def test_soft_muted_speech_does_not_publish_user_start(monkeypatch):
    async def run() -> None:
        class FakeProcessor:
            mode = handlers.VoiceMode.ACTIVE_AI_TURN
            turn_phase = handlers.SpeechTurnPhase.SPEAKING

            def __init__(self) -> None:
                self.turn_buffer = bytearray(b"muted-audio")

            async def add_audio(
                self,
                audio_bytes: bytes,
                *,
                retain_preroll: bool = True,
                preroll_seconds: float | None = None,
            ):
                return handlers.SpeechEvent.USER_TURN_STARTED

        class FakeTask:
            def done(self) -> bool:
                return False

        old_voice_turn = VoiceInputTurn(
            turn_id="old-turn",
            last_endpoint_monotonic=time.monotonic() - 5.0,
            transcript_text="old transcript",
        )
        session = SimpleNamespace(
            processor=FakeProcessor(),
            voice_turn=old_voice_turn,
            accepted_input_task=None,
            current_run_task=FakeTask(),
            endpoint_decision_task=None,
            first_audio_sent=True,
            soft_muted=True,
        )
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        fake_bus = SimpleNamespace(publish=AsyncMock())
        start_streaming = AsyncMock()
        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "event_bus", fake_bus)
        monkeypatch.setattr(handlers, "_start_streaming_stt", start_streaming)

        message = WSMessage(
            type=WSMessageType.USER_AUDIO,
            data={
                "audio": base64.b64encode(b"chunk").decode("ascii"),
                "encoding": "base64",
            },
        )

        await handlers.handle_audio_stream("test", message)

        assert session.voice_turn is not old_voice_turn
        fake_bus.publish.assert_not_awaited()
        start_streaming.assert_awaited_once_with("test", session, b"muted-audio")

    asyncio.run(run())


def test_normal_speech_start_reuses_wake_voice_turn(monkeypatch):
    async def run() -> None:
        class FakeProcessor:
            mode = handlers.VoiceMode.ACTIVE_IDLE
            turn_phase = handlers.SpeechTurnPhase.SPEAKING

            def __init__(self) -> None:
                self.turn_buffer = bytearray(b"wake-and-speech")

            async def add_audio(
                self,
                audio_bytes: bytes,
                *,
                retain_preroll: bool = True,
                preroll_seconds: float | None = None,
            ):
                return handlers.SpeechEvent.USER_TURN_STARTED

        voice_turn = VoiceInputTurn(turn_id="wake-turn")
        session = SimpleNamespace(
            processor=FakeProcessor(),
            voice_turn=voice_turn,
            accepted_input_task=None,
            current_run_task=None,
            endpoint_decision_task=None,
            first_audio_sent=False,
        )
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        fake_bus = SimpleNamespace(publish=AsyncMock())
        start_streaming = AsyncMock()
        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "event_bus", fake_bus)
        monkeypatch.setattr(handlers, "_start_streaming_stt", start_streaming)

        message = WSMessage(
            type=WSMessageType.USER_AUDIO,
            data={
                "audio": base64.b64encode(b"chunk").decode("ascii"),
                "encoding": "base64",
            },
        )

        await handlers.handle_audio_stream("test", message)

        assert session.voice_turn is voice_turn
        fake_bus.publish.assert_awaited_once()
        start_streaming.assert_awaited_once_with("test", session, b"wake-and-speech")

    asyncio.run(run())


def test_wake_word_prepares_tts_before_starting_stt(monkeypatch):
    async def run() -> None:
        call_order: list[str] = []

        class FakeProcessor:
            def __init__(self) -> None:
                self.turn_buffer = bytearray(b"wake-audio")

            async def add_audio(
                self,
                audio_bytes: bytes,
                *,
                retain_preroll: bool = True,
                preroll_seconds: float | None = None,
            ):
                return handlers.SpeechEvent.WAKE_WORD_DETECTED

        session = SimpleNamespace(
            processor=FakeProcessor(),
            voice_turn=None,
        )
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        fake_bus = SimpleNamespace(publish=AsyncMock())
        fake_tts = SimpleNamespace(
            prepare_for_turn=MagicMock(side_effect=lambda: call_order.append("tts"))
        )

        async def start_streaming(*args):
            call_order.append("stt")

        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "event_bus", fake_bus)
        monkeypatch.setattr(handlers, "tts", fake_tts)
        monkeypatch.setattr(handlers, "_start_streaming_stt", start_streaming)

        message = WSMessage(
            type=WSMessageType.USER_AUDIO,
            data={
                "audio": base64.b64encode(b"chunk").decode("ascii"),
                "encoding": "base64",
            },
        )

        await handlers.handle_audio_stream("test", message)

        fake_tts.prepare_for_turn.assert_called_once_with()
        assert call_order == ["tts", "stt"]
        fake_bus.publish.assert_awaited_once()

    asyncio.run(run())


def test_text_input_schedules_current_run_task(monkeypatch):
    async def run() -> None:
        session = SimpleNamespace(
            pending_attachments=[],
            endpoint_decision_task=None,
            accepted_input_task=None,
            current_run_task=None,
        )
        fake_manager = SimpleNamespace(get_session=lambda session_id: session)
        process_turn = AsyncMock()
        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "orchestrator", SimpleNamespace(process_turn=process_turn))
        monkeypatch.setattr(handlers, "require_llm_ready", lambda: None)

        await handlers.handle_text_input(
            "test",
            WSMessage(type=WSMessageType.USER_TEXT, data={"text": "hello"}),
        )

        assert session.current_run_task is not None
        await session.current_run_task
        process_turn.assert_awaited_once_with(
            connection_id="test",
            audio_bytes=None,
            text="hello",
            attachments=None,
        )

    asyncio.run(run())


def test_text_input_ignored_while_run_active(monkeypatch):
    async def run() -> None:
        active_task = MagicMock()
        active_task.done.return_value = False
        session = SimpleNamespace(
            pending_attachments=[],
            endpoint_decision_task=None,
            accepted_input_task=None,
            current_run_task=active_task,
        )
        fake_manager = SimpleNamespace(get_session=lambda session_id: session)
        process_turn = AsyncMock()
        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "orchestrator", SimpleNamespace(process_turn=process_turn))
        monkeypatch.setattr(handlers, "require_llm_ready", lambda: None)

        await handlers.handle_text_input(
            "test",
            WSMessage(type=WSMessageType.USER_TEXT, data={"text": "second turn"}),
        )

        assert session.current_run_task is active_task
        process_turn.assert_not_called()

    asyncio.run(run())


def test_resumed_speech_cancels_endpoint_task_and_keeps_stream(monkeypatch):
    async def run() -> None:
        class FakeProcessor:
            mode = handlers.VoiceMode.ACTIVE_IDLE
            turn_phase = handlers.SpeechTurnPhase.SPEAKING

            def __init__(self) -> None:
                self.turn_buffer = bytearray(b"same-turn-audio")

            async def add_audio(
                self,
                audio_bytes: bytes,
                *,
                retain_preroll: bool = True,
                preroll_seconds: float | None = None,
            ):
                return handlers.SpeechEvent.TURN_RESUMED

        class FakeTask:
            def __init__(self) -> None:
                self.cancelled_with = None

            def done(self) -> bool:
                return False

            def cancel(self, reason=None) -> None:
                self.cancelled_with = reason

        class FakeStream:
            stream_id = "stream-1"
            latest_text = "partial"

            def __init__(self) -> None:
                self.feed = AsyncMock()

        endpoint_task = FakeTask()
        stream = FakeStream()
        voice_turn = VoiceInputTurn(
            turn_id="turn-1",
            transcript_text="partial",
            endpoint_candidate_started_at=time.monotonic(),
            speech_ended_at=time.monotonic() - 0.5,
        )
        session = SimpleNamespace(
            processor=FakeProcessor(),
            voice_turn=voice_turn,
            stt_stream=stream,
            endpoint_decision_task=endpoint_task,
        )
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        monkeypatch.setattr(handlers, "manager", fake_manager)

        message = WSMessage(
            type=WSMessageType.USER_AUDIO,
            data={
                "audio": base64.b64encode(b"resumed-chunk").decode("ascii"),
                "encoding": "base64",
            },
        )

        await handlers.handle_audio_stream("test", message)

        assert endpoint_task.cancelled_with == "speech_resumed"
        assert session.endpoint_decision_task is None
        assert session.stt_stream is stream
        stream.feed.assert_awaited_once_with(b"resumed-chunk")
        assert voice_turn.endpoint_candidate_started_at == 0.0
        assert voice_turn.speech_ended_at == 0.0

    asyncio.run(run())


def test_stale_websocket_activity_replays_pending_alerts(monkeypatch):
    async def run() -> None:
        class StaleSession:
            owner_id = "geoff"
            connection_id = "conn-browser"
            presence = SimpleNamespace(node_id="browser")
            touched = False

            def is_fresh(self, *, max_age_s: float) -> bool:
                assert max_age_s == handlers.SESSION_FRESHNESS_SECONDS
                return False

            def touch(self) -> None:
                self.touched = True

        session = StaleSession()
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        fake_bus = SimpleNamespace(publish=AsyncMock())
        monkeypatch.setattr(handlers, "manager", fake_manager)
        monkeypatch.setattr(handlers, "event_bus", fake_bus)

        await handlers.handle_message(
            "geoff",
            WSMessage(type=WSMessageType.PING, data={}),
        )

        assert session.touched is True
        fake_bus.publish.assert_awaited_once()
        event = fake_bus.publish.await_args.args[0]
        assert event.type == EventType.SESSION_CONNECTED
        assert event.source == "websocket_freshness_recovered"
        assert event.data == {
            "owner_id": "geoff",
            "session_id": "geoff",
            "connection_id": "conn-browser",
            "node_id": "browser",
        }

    asyncio.run(run())


def test_context_update_does_not_override_presence_identity(monkeypatch):
    async def run() -> None:
        session = SimpleNamespace(
            context={
                "owner_id": "geoff",
                "connection_id": "conn-browser",
                "node_id": "browser",
                "timezone": "UTC",
            }
        )
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        monkeypatch.setattr(handlers, "manager", fake_manager)

        await handlers.handle_context_update(
            "conn-browser",
            WSMessage(
                type=WSMessageType.CONTEXT_UPDATE,
                data={
                    "owner_id": "spoofed",
                    "connection_id": "spoofed-conn",
                    "node_id": "spoofed-node",
                    "timezone": "Australia/Sydney",
                    "location": {"latitude": -33.86, "longitude": 151.2},
                },
            ),
        )

        assert session.context["owner_id"] == "geoff"
        assert session.context["connection_id"] == "conn-browser"
        assert session.context["node_id"] == "browser"
        assert session.context["timezone"] == "Australia/Sydney"
        assert session.context["location"]["latitude"] == -33.86
        assert session.context["location"]["longitude"] == 151.2
        assert session.context["location"]["source"] == "gps"
        assert session.context["location"]["captured_at"]

    asyncio.run(run())


def test_context_update_rejects_invalid_location_without_mutating_session(monkeypatch):
    async def run() -> None:
        session = SimpleNamespace(context={"timezone": "UTC"})
        fake_manager = SimpleNamespace(
            get_session=lambda session_id: session,
            send_message=AsyncMock(),
        )
        monkeypatch.setattr(handlers, "manager", fake_manager)

        await handlers.handle_context_update(
            "conn-browser",
            WSMessage(
                type=WSMessageType.CONTEXT_UPDATE,
                data={"location": {"latitude": 200, "longitude": 151.2}},
            ),
        )

        assert "location" not in session.context
        response = fake_manager.send_message.await_args.args[1]
        assert response.data == {
            "status": "context_update_rejected",
            "error": "invalid_location",
        }

    asyncio.run(run())


def test_turn_detector_max_delay_commits(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=False, confidence=0.1, reason="fake_continue")

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_max_delay", 0.1)

        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-1",
                transcript_text="I want to",
                endpoint_candidate_started_at=time.monotonic() - 1.0,
                endpoint_candidate_text_chars=len("I want to"),
            ),
            stt_stream=None,
        )

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)
        assert should_commit is True
        assert decision.reason == "max_delay:fake_continue"

    asyncio.run(run())


def test_turn_detector_max_delay_resets_when_transcript_advances(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=False, confidence=0.1, reason="fake_continue")

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_max_delay", 0.1)

        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-1",
                transcript_text="I want to keep talking",
                endpoint_candidate_started_at=time.monotonic() - 1.0,
                endpoint_candidate_text_chars=len("I want to"),
            ),
            stt_stream=None,
        )

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)
        assert should_commit is False
        assert decision.reason == "fake_continue"

    asyncio.run(run())


def test_endpoint_min_delay_is_measured_from_last_speech(monkeypatch):
    monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_min_delay", 0.15)

    already_waited = VoiceInputTurn(
        turn_id="turn-waited",
        speech_ended_at=time.monotonic() - 0.2,
    )
    assert handlers._endpoint_min_delay_remaining(already_waited) == 0.0

    recent_speech = VoiceInputTurn(
        turn_id="turn-recent",
        speech_ended_at=time.monotonic() - 0.1,
    )
    assert 0.03 <= handlers._endpoint_min_delay_remaining(recent_speech) <= 0.06


def test_endpoint_resolver_waits_min_delay_for_late_text(monkeypatch):
    async def run() -> None:
        predict_calls = 0

        class FakeDetector:
            async def predict(self, *, language="en"):
                nonlocal predict_calls
                predict_calls += 1
                return TurnDecision(done=True, confidence=0.9, reason="fake_done")

        class FakeStream:
            stream_id = "stream-1"
            bytes_fed = 0
            feed_count = 0

            def __init__(self) -> None:
                self.latest_text = ""

            async def finish(self):
                return self.latest_text

        stream = FakeStream()

        async def publish_late_text() -> None:
            await asyncio.sleep(0.01)
            stream.latest_text = "It's good to hear, buddy."

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_min_delay", 0.03)
        monkeypatch.setattr(handlers, "orchestrator", SimpleNamespace(process_turn=AsyncMock()))

        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(turn_id="turn-1", endpoint_candidate_started_at=time.monotonic()),
            stt_stream=stream,
            processor=SimpleNamespace(
                turn_phase=handlers.SpeechTurnPhase.ENDPOINT_CANDIDATE,
                turn_buffer=bytearray(b"audio"),
                consume_turn_audio=MagicMock(return_value=b"audio"),
            ),
            endpoint_decision_task=None,
            accepted_input_task=None,
            current_run_task=None,
            pending_attachments=[],
            turn_detector=None,
        )

        publisher = asyncio.create_task(publish_late_text())
        task = asyncio.create_task(handlers._resolve_endpoint_candidate("test", session, session.voice_turn))
        session.endpoint_decision_task = task
        await task
        await publisher

        assert predict_calls >= 1
        assert session.voice_turn.transcript_text == "It's good to hear, buddy."
        assert session.current_run_task is not None
        handlers.orchestrator.process_turn.assert_called_once()

    asyncio.run(run())


def test_endpoint_resolver_ignores_stale_task(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=True, confidence=0.9, reason="fake_done")

        class FakeStream:
            stream_id = "stream-1"
            latest_text = "Complete sentence."

            def __init__(self) -> None:
                self.finished = False

            async def finish(self):
                self.finished = True
                return self.latest_text

        stream = FakeStream()
        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_min_delay", 0.01)
        monkeypatch.setattr(handlers, "orchestrator", SimpleNamespace(process_turn=AsyncMock()))

        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(turn_id="turn-1", transcript_text="Complete sentence."),
            stt_stream=stream,
            processor=SimpleNamespace(
                turn_phase=handlers.SpeechTurnPhase.ENDPOINT_CANDIDATE,
                turn_buffer=bytearray(b"audio"),
                consume_turn_audio=MagicMock(return_value=b"audio"),
            ),
            endpoint_decision_task=None,
            accepted_input_task=None,
            current_run_task=None,
            pending_attachments=[],
        )

        task = asyncio.create_task(handlers._resolve_endpoint_candidate("test", session, session.voice_turn))
        stale_replacement = asyncio.create_task(asyncio.sleep(0.05))
        session.endpoint_decision_task = stale_replacement
        await task
        stale_replacement.cancel()

        assert stream.finished is False
        assert session.current_run_task is None
        handlers.orchestrator.process_turn.assert_not_called()

    asyncio.run(run())


def test_endpoint_resolver_registers_accepted_input_during_commit(monkeypatch):
    async def run() -> None:
        finish_started = asyncio.Event()
        finish_released = asyncio.Event()

        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=True, confidence=0.9, reason="fake_done")

        class FakeStream:
            stream_id = "stream-1"
            bytes_fed = 0
            feed_count = 0
            latest_text = "Complete sentence."

            async def finish(self):
                finish_started.set()
                await finish_released.wait()
                return self.latest_text

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_min_delay", 0.0)
        monkeypatch.setattr(handlers, "orchestrator", SimpleNamespace(process_turn=AsyncMock()))

        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(turn_id="turn-1", transcript_text="Complete sentence."),
            stt_stream=FakeStream(),
            processor=SimpleNamespace(
                turn_phase=handlers.SpeechTurnPhase.ENDPOINT_CANDIDATE,
                turn_buffer=bytearray(b"audio"),
                consume_turn_audio=MagicMock(return_value=b"audio"),
            ),
            endpoint_decision_task=None,
            accepted_input_task=None,
            current_run_task=None,
            pending_attachments=[],
        )

        task = asyncio.create_task(handlers._resolve_endpoint_candidate("test", session, session.voice_turn))
        session.endpoint_decision_task = task
        await finish_started.wait()

        # During commit (inside _commit_voice_turn's STT finish await), the endpoint
        # task registers itself as accepted_input_task.
        assert session.accepted_input_task is task

        finish_released.set()
        await task

        # After commit completes, accepted_input_task is cleared by the finally block.
        assert session.accepted_input_task is None
        # process_turn was scheduled into current_run_task.
        handlers.orchestrator.process_turn.assert_called_once()
        if session.current_run_task is not None:
            await session.current_run_task

    asyncio.run(run())


def test_eou_visibility_fields_and_transcript_stamp():
    now = 1000.0
    voice_turn = VoiceInputTurn(
        turn_id="turn-vis",
        speech_ended_at=now - 0.4,
        first_transcript_at=now - 0.4,
        continue_count=2,
        awaiting_stt_count=1,
        vad_endpoint_count=3,
    )
    fields = handlers._eou_visibility_fields(voice_turn, now=now)
    assert fields["end_of_turn_delay_ms"] == 400.0
    assert fields["transcription_delay_ms"] == 0.0
    assert fields["continue_count"] == 2
    assert fields["awaiting_stt_count"] == 1
    assert fields["vad_endpoint_count"] == 3

    late = VoiceInputTurn(
        turn_id="turn-late",
        speech_ended_at=now - 0.5,
        first_transcript_at=now - 0.2,
    )
    late_fields = handlers._eou_visibility_fields(late, now=now)
    assert late_fields["transcription_delay_ms"] == 300.0

    stamped = VoiceInputTurn(turn_id="turn-stamp")
    assert handlers._apply_voice_turn_transcript(
        "s",
        stamped,
        "Jarvis lights off",
        event="stt_transcript_sync",
        reason="test",
    )
    assert stamped.first_transcript_at > 0
    first_at = stamped.first_transcript_at
    assert handlers._apply_voice_turn_transcript(
        "s",
        stamped,
        "Jarvis lights off please",
        event="stt_transcript_sync",
        reason="test",
    )
    assert stamped.first_transcript_at == first_at


def test_endpoint_resolver_polls_for_late_stt_without_continue_turn(monkeypatch):
    async def run() -> None:
        predict_calls = 0

        class FakeDetector:
            async def predict(self, *, language="en"):
                nonlocal predict_calls
                predict_calls += 1
                return TurnDecision(done=True, confidence=0.9, reason="fake_done")

        class FakeStream:
            stream_id = "stream-1"
            bytes_fed = 0
            feed_count = 0

            def __init__(self) -> None:
                self.latest_text = ""

            async def finish(self):
                return self.latest_text

        stream = FakeStream()
        continue_turn = MagicMock()

        async def publish_late_text() -> None:
            await asyncio.sleep(0.04)
            stream.latest_text = "Have you?"

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_min_delay", 0.0)
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_awaiting_stt_timeout", 0.2)
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_max_delay", 10.0)
        monkeypatch.setattr(handlers, "orchestrator", SimpleNamespace(process_turn=AsyncMock()))

        voice_turn = VoiceInputTurn(
            turn_id="turn-1",
            endpoint_candidate_started_at=time.monotonic(),
            speech_ended_at=time.monotonic(),
            vad_endpoint_count=1,
        )
        session = SimpleNamespace(
            voice_turn=voice_turn,
            stt_stream=stream,
            processor=SimpleNamespace(
                turn_phase=handlers.SpeechTurnPhase.ENDPOINT_CANDIDATE,
                turn_buffer=bytearray(b"audio"),
                continue_turn=continue_turn,
                consume_turn_audio=MagicMock(return_value=b"audio"),
            ),
            endpoint_decision_task=None,
            accepted_input_task=None,
            current_run_task=None,
            pending_attachments=[],
            turn_detector=None,
        )

        publisher = asyncio.create_task(publish_late_text())
        task = asyncio.create_task(handlers._resolve_endpoint_candidate("test", session, voice_turn))
        session.endpoint_decision_task = task
        await task
        await publisher

        continue_turn.assert_not_called()
        assert voice_turn.continue_count == 0
        assert voice_turn.awaiting_stt_count == 0
        assert voice_turn.transcript_text == "Have you?"
        assert predict_calls >= 1
        handlers.orchestrator.process_turn.assert_called_once()

    asyncio.run(run())


def test_endpoint_resolver_counts_awaiting_stt_continue(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                raise AssertionError("should continue before predict when STT text missing")

        continue_turn = MagicMock()
        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_min_delay", 0.0)
        # Expire the in-candidate poll immediately so we fall through to continue_turn.
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_awaiting_stt_timeout", 0.0)
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_max_delay", 10.0)

        speech_ended_at = time.monotonic() - 0.25
        voice_turn = VoiceInputTurn(
            turn_id="turn-await",
            endpoint_candidate_started_at=time.monotonic(),
            speech_ended_at=speech_ended_at,
            vad_endpoint_count=1,
        )
        session = SimpleNamespace(
            voice_turn=voice_turn,
            stt_stream=SimpleNamespace(latest_text="", stream_id="stt-1"),
            processor=SimpleNamespace(
                turn_phase=handlers.SpeechTurnPhase.ENDPOINT_CANDIDATE,
                turn_buffer=bytearray(b"audio"),
                continue_turn=continue_turn,
                consume_turn_audio=MagicMock(return_value=b"audio"),
            ),
            endpoint_decision_task=None,
            accepted_input_task=None,
            current_run_task=None,
            pending_attachments=[],
            turn_detector=None,
        )

        task = asyncio.create_task(handlers._resolve_endpoint_candidate("test", session, voice_turn))
        session.endpoint_decision_task = task
        await task

        continue_turn.assert_called_once_with(reason="turn_detector_continue")
        assert voice_turn.continue_count == 1
        assert voice_turn.awaiting_stt_count == 1
        assert voice_turn.speech_ended_at == speech_ended_at

    asyncio.run(run())


def test_wake_only_transcript_continues_for_followon(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                raise AssertionError("wake-only should not reach audio EOU")

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_max_delay", 3.0)

        now = time.monotonic()
        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-wake",
                transcript_text="Jarvis",
                from_wake=True,
                endpoint_candidate_started_at=now - 0.2,
                endpoint_candidate_text_chars=len("Jarvis"),
            ),
            stt_stream=None,
        )

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)
        assert should_commit is False
        assert decision.reason == "wake_followon_pending"

    asyncio.run(run())


def test_wake_plus_request_reaches_eou(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=True, confidence=0.9, reason="audio_eou")

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_max_delay", 3.0)

        text = "Jarvis turn off the lights"
        now = time.monotonic()
        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-wake-cmd",
                transcript_text=text,
                from_wake=True,
                endpoint_candidate_started_at=now - 0.2,
                endpoint_candidate_text_chars=len(text),
            ),
            stt_stream=None,
        )

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)
        assert should_commit is True
        assert decision.reason == "audio_eou"

    asyncio.run(run())


def test_wake_followon_timeout_settles_without_llm(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                raise AssertionError("wake-only timeout should not reach audio EOU")

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_max_delay", 0.1)

        now = time.monotonic()
        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-wake-timeout",
                transcript_text="hey jarvis",
                from_wake=True,
                endpoint_candidate_started_at=now - 1.0,
                endpoint_candidate_text_chars=len("hey jarvis"),
            ),
            stt_stream=None,
        )

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)
        assert should_commit is True
        assert decision.reason == "wake_followon_timeout"

    asyncio.run(run())


def test_non_wake_short_turn_still_commits_fast(monkeypatch):
    async def run() -> None:
        class FakeDetector:
            async def predict(self, *, language="en"):
                return TurnDecision(done=True, confidence=0.95, reason="audio_eou")

        monkeypatch.setattr(handlers, "_ensure_turn_detector", lambda session: FakeDetector())
        monkeypatch.setattr(handlers.settings.VOICE, "turn_detector_max_delay", 3.0)

        now = time.monotonic()
        session = SimpleNamespace(
            voice_turn=VoiceInputTurn(
                turn_id="turn-short",
                transcript_text="yes",
                from_wake=False,
                endpoint_candidate_started_at=now - 0.05,
                endpoint_candidate_text_chars=3,
            ),
            stt_stream=None,
        )

        should_commit, decision = await handlers._should_commit_voice_turn("test", session)
        assert should_commit is True
        assert decision.reason == "audio_eou"

    asyncio.run(run())
