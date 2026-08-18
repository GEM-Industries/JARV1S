import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.websockets import handlers
from api.websockets.connection import VoiceInputTurn
from api.websockets.models import WSMessage
from api.websockets.types import WSMessageType
from core.voice.processor import SpeechTurnPhase
from core.voice.speaker_verifier import SpeakerEvidence, SpeakerMatchStatus
from core.voice.turn_admission import AdmissionAction
from core.voice.turn_detector import TurnDecision
from services.events import EventType


@pytest.mark.asyncio
async def test_handler_max_wait_timer_commits_candidate_turn(monkeypatch) -> None:
    candidate_turn = VoiceInputTurn(turn_id="turn-candidate")
    session = SimpleNamespace(
        voice_turn=None,
        barge_in_candidate_turn=candidate_turn,
        stt_stream=None,
        current_trigger_instance_id=None,
        barge_in_candidate_started_at=time.monotonic() - 2.0,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_attempts=0,
        processor=SimpleNamespace(peek_turn_speech_audio=MagicMock(return_value=b"")),
    )
    publish_user_start = AsyncMock()
    send_voice_response = AsyncMock()
    monkeypatch.setattr(handlers.settings.VOICE, "barge_in_candidate_max_wait_s", 0.001)
    monkeypatch.setattr(handlers, "_publish_voice_user_start", publish_user_start)
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=send_voice_response),
    )

    task = asyncio.create_task(
        handlers._barge_in_candidate_max_wait("test", session, candidate_turn)
    )
    session.barge_in_candidate_task = task
    await task

    assert session.voice_turn is candidate_turn
    assert session.barge_in_candidate_committed is True
    assert session.barge_in_candidate_task is None
    publish_user_start.assert_awaited_once_with("test", session)
    send_voice_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_suppresses_proactive_side_speech(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="okay okay i will check",
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_turn=voice_turn,
        stt_stream=None,
        current_trigger_instance_id="trg-1",
        barge_in_candidate_started_at=time.monotonic() - 0.3,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_attempts=0,
        processor=SimpleNamespace(
            peek_turn_speech_audio=MagicMock(return_value=b""),
            suppress_barge_in_candidate=MagicMock(),
        ),
    )

    close_streaming = AsyncMock()
    discard_latency = MagicMock()
    send_message = AsyncMock()
    monkeypatch.setattr(handlers, "_close_streaming_stt", close_streaming)
    monkeypatch.setattr(handlers, "_discard_turn_latency", discard_latency)
    monkeypatch.setattr(handlers, "manager", SimpleNamespace(send_message=send_message))

    decision = await handlers._resolve_barge_in_candidate("test", session, endpointed=True)

    assert decision is AdmissionAction.SUPPRESS
    close_streaming.assert_awaited_once()
    discard_latency.assert_called_once()
    session.processor.suppress_barge_in_candidate.assert_called_once()
    assert session.barge_in_candidate_started_at == 0.0
    assert session.barge_in_candidate_turn is None
    send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_commits_normal_answer_text(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="actually tell me the next one",
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_turn=voice_turn,
        stt_stream=None,
        current_trigger_instance_id=None,
        barge_in_candidate_started_at=time.monotonic() - 0.3,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_task=None,
        barge_in_speaker_evidence=None,
        barge_in_speaker_attempts=0,
        speaker_verifier=None,
        processor=SimpleNamespace(peek_turn_speech_audio=MagicMock(return_value=b"")),
    )

    async def publish_user_start(session_id: str, current_session) -> None:
        current_session.voice_turn = None

    send_voice_response = AsyncMock()
    publish_user_start_mock = AsyncMock(side_effect=publish_user_start)
    monkeypatch.setattr(handlers, "_publish_voice_user_start", publish_user_start_mock)
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=send_voice_response),
    )

    decision = await handlers._resolve_barge_in_candidate("test", session, endpointed=True)

    assert decision is AdmissionAction.COMMIT
    publish_user_start_mock.assert_awaited_once_with("test", session)
    send_voice_response.assert_awaited_once()
    assert session.voice_turn is voice_turn
    assert session.barge_in_candidate_committed is True


@pytest.mark.asyncio
async def test_handler_suppresses_enrolled_mismatch(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="actually tell me the next one",
    )
    speaker = SimpleNamespace(enrolled=True)
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_turn=voice_turn,
        stt_stream=None,
        current_trigger_instance_id=None,
        barge_in_candidate_started_at=time.monotonic() - 2.0,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_task=None,
        barge_in_speaker_evidence=None,
        barge_in_speaker_attempts=0,
        speaker_verifier=speaker,
        processor=SimpleNamespace(
            peek_turn_speech_audio=MagicMock(return_value=b"\x00\x01" * 1600),
            suppress_barge_in_candidate=MagicMock(),
        ),
    )
    evidence = SpeakerEvidence(
        status=SpeakerMatchStatus.MISMATCH,
        speaker_id="owner-a",
        cosine=0.2,
        threshold=0.6,
    )

    async def ensure(_session, *, rescore: bool = False):
        _session.barge_in_speaker_evidence = evidence
        _session.barge_in_speaker_attempts = 2 if rescore else 1
        return evidence

    close_streaming = AsyncMock()
    discard_latency = MagicMock()
    publish_user_start = AsyncMock()
    send_message = AsyncMock()
    monkeypatch.setattr(handlers, "_ensure_barge_in_speaker_evidence", ensure)
    monkeypatch.setattr(handlers, "_close_streaming_stt", close_streaming)
    monkeypatch.setattr(handlers, "_discard_turn_latency", discard_latency)
    monkeypatch.setattr(handlers, "_publish_voice_user_start", publish_user_start)
    monkeypatch.setattr(handlers, "manager", SimpleNamespace(send_message=send_message))

    decision = await handlers._resolve_barge_in_candidate("test", session, endpointed=True)

    assert decision is AdmissionAction.SUPPRESS
    publish_user_start.assert_not_awaited()
    session.processor.suppress_barge_in_candidate.assert_called_once()
    assert session.barge_in_candidate_started_at == 0.0
    send_message.assert_awaited_once()
    retract = send_message.await_args.args[1]
    assert retract.type == handlers.WSMessageType.RETRACT
    assert retract.data == {"message_id": "turn-1"}


@pytest.mark.asyncio
async def test_handler_early_endpoint_mismatch_waits_then_retries_at_max_wait(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="actually tell me the next one",
    )
    early = SpeakerEvidence(
        status=SpeakerMatchStatus.MISMATCH,
        speaker_id="owner-a",
        cosine=0.2,
        threshold=0.6,
    )
    final = SpeakerEvidence(
        status=SpeakerMatchStatus.MATCHED,
        speaker_id="owner-a",
        cosine=0.81,
        threshold=0.6,
    )
    speaker = SimpleNamespace(
        enrolled=True,
        verify_pcm=MagicMock(side_effect=[early, final]),
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_turn=voice_turn,
        stt_stream=None,
        current_trigger_instance_id=None,
        barge_in_candidate_started_at=time.monotonic() - 0.4,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_task=None,
        barge_in_speaker_evidence=None,
        barge_in_speaker_attempts=0,
        speaker_verifier=speaker,
        processor=SimpleNamespace(
            peek_turn_speech_audio=MagicMock(side_effect=[b"early", b"complete", b"complete"]),
        ),
    )
    send_voice_response = AsyncMock()
    publish_user_start = AsyncMock()
    monkeypatch.setattr(handlers, "_publish_voice_user_start", publish_user_start)
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=send_voice_response),
    )

    early_decision = await handlers._resolve_barge_in_candidate(
        "test", session, endpointed=True
    )
    assert early_decision is AdmissionAction.WAIT
    assert session.barge_in_speaker_attempts == 1
    assert speaker.verify_pcm.call_count == 1

    session.barge_in_candidate_started_at = time.monotonic() - 2.0
    decision = await handlers._resolve_barge_in_candidate("test", session, endpointed=True)

    assert decision is AdmissionAction.COMMIT
    assert speaker.verify_pcm.call_count == 2
    assert voice_turn.speaker_id == "owner-a"
    assert voice_turn.speaker_confidence == pytest.approx(0.81)
    publish_user_start.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_commits_enrolled_owner_and_stamps_identity(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="actually tell me the next one",
    )
    speaker = SimpleNamespace(enrolled=True)
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_turn=voice_turn,
        stt_stream=None,
        current_trigger_instance_id=None,
        barge_in_candidate_started_at=time.monotonic() - 0.3,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_task=None,
        barge_in_speaker_evidence=None,
        barge_in_speaker_attempts=0,
        speaker_verifier=speaker,
        processor=SimpleNamespace(
            peek_turn_speech_audio=MagicMock(return_value=b"\x00\x01" * 1600),
        ),
    )
    evidence = SpeakerEvidence(
        status=SpeakerMatchStatus.MATCHED,
        speaker_id="owner-a",
        cosine=0.88,
        threshold=0.6,
    )

    async def ensure(_session, *, rescore: bool = False):
        _session.barge_in_speaker_evidence = evidence
        _session.barge_in_speaker_attempts = 1
        return evidence

    send_voice_response = AsyncMock()
    publish_user_start = AsyncMock()
    monkeypatch.setattr(handlers, "_ensure_barge_in_speaker_evidence", ensure)
    monkeypatch.setattr(handlers, "_publish_voice_user_start", publish_user_start)
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=send_voice_response),
    )

    decision = await handlers._resolve_barge_in_candidate("test", session, endpointed=True)

    assert decision is AdmissionAction.COMMIT
    publish_user_start.assert_awaited_once()
    assert voice_turn.speaker_id == "owner-a"
    assert voice_turn.speaker_confidence == pytest.approx(0.88)
    assert voice_turn.speaker_source == "barge_in"


@pytest.mark.asyncio
async def test_handler_reuses_one_speaker_inference(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="hold on a second",
    )
    evidence = SpeakerEvidence(
        status=SpeakerMatchStatus.MISMATCH,
        speaker_id="owner-a",
        cosine=0.15,
        threshold=0.6,
    )
    speaker = SimpleNamespace(
        enrolled=True,
        verify_pcm=MagicMock(return_value=evidence),
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_turn=voice_turn,
        stt_stream=None,
        current_trigger_instance_id=None,
        barge_in_candidate_started_at=time.monotonic() - 2.0,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_task=None,
        barge_in_speaker_evidence=None,
        barge_in_speaker_attempts=0,
        speaker_verifier=speaker,
        processor=SimpleNamespace(
            peek_turn_speech_audio=MagicMock(return_value=b"\x00\x01" * 1600),
            suppress_barge_in_candidate=MagicMock(),
        ),
    )
    send_message = AsyncMock()
    monkeypatch.setattr(handlers, "_close_streaming_stt", AsyncMock())
    monkeypatch.setattr(handlers, "_discard_turn_latency", MagicMock())
    monkeypatch.setattr(handlers, "manager", SimpleNamespace(send_message=send_message))

    first, second = await asyncio.gather(
        handlers._resolve_barge_in_candidate("test", session, endpointed=True),
        handlers._resolve_barge_in_candidate("test", session, endpointed=True),
    )

    assert first is AdmissionAction.SUPPRESS
    assert second in {AdmissionAction.SUPPRESS, AdmissionAction.WAIT}
    # First negative at max-wait is rescored once, so two inferences is the cap.
    assert speaker.verify_pcm.call_count == 2


@pytest.mark.asyncio
async def test_suppressed_candidate_preserves_existing_voice_turn(monkeypatch) -> None:
    prior_turn = VoiceInputTurn(
        turn_id="turn-prior",
        transcript_text="I can get a lot of progress done",
    )
    candidate_turn = VoiceInputTurn(
        turn_id="turn-candidate",
        transcript_text="",
    )
    session = SimpleNamespace(
        voice_turn=prior_turn,
        barge_in_candidate_turn=candidate_turn,
        stt_stream=None,
        current_trigger_instance_id=None,
        barge_in_candidate_started_at=time.monotonic() - 0.3,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_attempts=0,
        processor=SimpleNamespace(
            peek_turn_speech_audio=MagicMock(return_value=b""),
            suppress_barge_in_candidate=MagicMock(),
        ),
    )

    close_streaming = AsyncMock()
    discard_latency = MagicMock()
    send_message = AsyncMock()
    monkeypatch.setattr(handlers, "_close_streaming_stt", close_streaming)
    monkeypatch.setattr(handlers, "_discard_turn_latency", discard_latency)
    monkeypatch.setattr(handlers, "manager", SimpleNamespace(send_message=send_message))

    decision = await handlers._resolve_barge_in_candidate("test", session, endpointed=True)

    assert decision is AdmissionAction.SUPPRESS
    assert session.voice_turn is prior_turn
    assert session.barge_in_candidate_turn is None
    discard_latency.assert_called_once_with(
        "test",
        candidate_turn,
        reason="barge_in_suppressed:empty_or_tiny",
    )
    send_message.assert_awaited_once()
    assert send_message.await_args.args[1].data == {"message_id": "turn-candidate"}


@pytest.mark.asyncio
async def test_fast_recovery_reuses_existing_turn_before_barge_candidate(monkeypatch) -> None:
    prior_turn = VoiceInputTurn(
        turn_id="turn-prior",
        transcript_text="I can get a lot of progress done",
        last_endpoint_monotonic=time.monotonic() - 0.2,
    )
    run_task = asyncio.create_task(asyncio.sleep(30))
    session = SimpleNamespace(
        voice_turn=prior_turn,
        accepted_input_task=None,
        current_run_task=run_task,
        current_delivery=SimpleNamespace(response_id="response-1", signal_cancel=MagicMock()),
        stt_stream=None,
        processor=SimpleNamespace(turn_buffer=bytearray(b"more speech")),
    )
    send_message = AsyncMock()
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_message=send_message),
    )

    try:
        recovered = await handlers._maybe_start_fast_recovery("test", session, message_id="msg-1")
    finally:
        run_task.cancel()

    assert recovered is True
    assert session.voice_turn is prior_turn
    assert prior_turn.continuation_prefix == "I can get a lot of progress done"
    session.current_delivery.signal_cancel.assert_called_once()
    assert send_message.await_count == 2


def _endpoint_handoff_session(
    *,
    transcript: str,
    turn_phase: SpeechTurnPhase,
    started_ago_s: float = 0.4,
) -> tuple[SimpleNamespace, VoiceInputTurn]:
    voice_turn = VoiceInputTurn(turn_id="turn-1", transcript_text=transcript)
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_turn=voice_turn,
        stt_stream=None,
        current_trigger_instance_id=None,
        barge_in_candidate_started_at=time.monotonic() - started_ago_s,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_task=None,
        barge_in_speaker_evidence=None,
        barge_in_speaker_attempts=0,
        speaker_verifier=None,
        endpoint_decision_task=None,
        processor=SimpleNamespace(
            turn_phase=turn_phase,
            peek_turn_speech_audio=MagicMock(return_value=b""),
        ),
    )
    return session, voice_turn


@pytest.mark.asyncio
async def test_soft_wait_commit_hands_off_endpointed_turn(monkeypatch) -> None:
    session, voice_turn = _endpoint_handoff_session(
        transcript="Yeah.",
        turn_phase=SpeechTurnPhase.ENDPOINT_CANDIDATE,
        started_ago_s=0.4,
    )
    session.barge_in_speaker_evidence = SpeakerEvidence(
        status=SpeakerMatchStatus.MATCHED,
        speaker_id="owner-a",
        cosine=0.29,
        threshold=0.21,
    )
    session.speaker_verifier = SimpleNamespace(enrolled=True)
    session.barge_in_speaker_attempts = 1

    publish = AsyncMock()
    schedule = MagicMock()
    monkeypatch.setattr(handlers, "_publish_voice_user_start", AsyncMock())
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=AsyncMock()),
    )
    monkeypatch.setattr(handlers, "event_bus", SimpleNamespace(publish=publish))
    monkeypatch.setattr(handlers, "_schedule_endpoint_decision", schedule)

    first = await handlers._resolve_barge_in_candidate(
        "test",
        session,
        endpointed=True,
    )
    # Already committed path: simulate deferred soft-wait by ensuring COMMIT.
    assert first is AdmissionAction.COMMIT
    publish.assert_awaited_once()
    assert publish.await_args.args[0].type is EventType.VOICE_USER_END
    schedule.assert_called_once_with("test", session, voice_turn)


@pytest.mark.asyncio
async def test_soft_wait_wait_then_commit_hands_off_once(monkeypatch) -> None:
    session, voice_turn = _endpoint_handoff_session(
        transcript="",
        turn_phase=SpeechTurnPhase.ENDPOINT_CANDIDATE,
        started_ago_s=0.3,
    )
    session.speaker_verifier = SimpleNamespace(enrolled=True)
    session.barge_in_speaker_evidence = SpeakerEvidence(
        status=SpeakerMatchStatus.MATCHED,
        speaker_id="owner-a",
        cosine=0.4,
        threshold=0.21,
    )
    session.barge_in_speaker_attempts = 1

    publish = AsyncMock()
    schedule = MagicMock()
    monkeypatch.setattr(handlers, "_publish_voice_user_start", AsyncMock())
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=AsyncMock()),
    )
    monkeypatch.setattr(handlers, "event_bus", SimpleNamespace(publish=publish))
    monkeypatch.setattr(handlers, "_schedule_endpoint_decision", schedule)

    waiting = await handlers._resolve_barge_in_candidate(
        "test", session, endpointed=True
    )
    assert waiting is AdmissionAction.WAIT
    publish.assert_not_awaited()
    schedule.assert_not_called()

    voice_turn.transcript_text = "Yeah."
    committed = await handlers._resolve_barge_in_candidate(
        "test", session, endpointed=True
    )
    assert committed is AdmissionAction.COMMIT
    publish.assert_awaited_once()
    schedule.assert_called_once_with("test", session, voice_turn)


@pytest.mark.asyncio
async def test_max_wait_endpoint_candidate_hands_off(monkeypatch) -> None:
    session, voice_turn = _endpoint_handoff_session(
        transcript="stop talking please",
        turn_phase=SpeechTurnPhase.ENDPOINT_CANDIDATE,
        started_ago_s=2.0,
    )
    session.speaker_verifier = SimpleNamespace(enrolled=True)
    session.barge_in_speaker_evidence = SpeakerEvidence(
        status=SpeakerMatchStatus.MATCHED,
        speaker_id="owner-a",
        cosine=0.5,
        threshold=0.21,
    )
    session.barge_in_speaker_attempts = 1

    publish = AsyncMock()
    schedule = MagicMock()
    monkeypatch.setattr(handlers, "_publish_voice_user_start", AsyncMock())
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=AsyncMock()),
    )
    monkeypatch.setattr(handlers, "event_bus", SimpleNamespace(publish=publish))
    monkeypatch.setattr(handlers, "_schedule_endpoint_decision", schedule)
    monkeypatch.setattr(handlers.settings.VOICE, "barge_in_candidate_max_wait_s", 0.001)

    task = asyncio.create_task(
        handlers._barge_in_candidate_max_wait("test", session, voice_turn)
    )
    session.barge_in_candidate_task = task
    await task

    assert session.barge_in_candidate_committed is True
    publish.assert_awaited_once()
    schedule.assert_called_once_with("test", session, voice_turn)


@pytest.mark.asyncio
async def test_speaking_max_wait_commit_does_not_hand_off(monkeypatch) -> None:
    session, voice_turn = _endpoint_handoff_session(
        transcript="keep going with this",
        turn_phase=SpeechTurnPhase.SPEAKING,
        started_ago_s=2.0,
    )
    session.speaker_verifier = SimpleNamespace(enrolled=True)
    session.barge_in_speaker_evidence = SpeakerEvidence(
        status=SpeakerMatchStatus.MATCHED,
        speaker_id="owner-a",
        cosine=0.5,
        threshold=0.21,
    )
    session.barge_in_speaker_attempts = 1

    publish = AsyncMock()
    schedule = MagicMock()
    monkeypatch.setattr(handlers, "_publish_voice_user_start", AsyncMock())
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=AsyncMock()),
    )
    monkeypatch.setattr(handlers, "event_bus", SimpleNamespace(publish=publish))
    monkeypatch.setattr(handlers, "_schedule_endpoint_decision", schedule)

    decision = await handlers._resolve_barge_in_candidate(
        "test", session, endpointed=False
    )
    assert decision is AdmissionAction.COMMIT
    assert session.barge_in_candidate_committed is True
    publish.assert_not_awaited()
    schedule.assert_not_called()
    assert session.endpoint_decision_task is None


@pytest.mark.asyncio
async def test_direct_endpointed_commit_hands_off_exactly_once(monkeypatch) -> None:
    session, voice_turn = _endpoint_handoff_session(
        transcript="actually tell me the next one",
        turn_phase=SpeechTurnPhase.ENDPOINT_CANDIDATE,
    )
    publish = AsyncMock()
    schedule = MagicMock()
    monkeypatch.setattr(handlers, "_publish_voice_user_start", AsyncMock())
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=AsyncMock()),
    )
    monkeypatch.setattr(handlers, "event_bus", SimpleNamespace(publish=publish))
    monkeypatch.setattr(handlers, "_schedule_endpoint_decision", schedule)

    decision = await handlers._resolve_barge_in_candidate(
        "test", session, endpointed=True
    )
    assert decision is AdmissionAction.COMMIT
    assert handlers._barge_in_candidate_active(session) is False
    publish.assert_awaited_once()
    assert publish.await_args.args[0].type is EventType.VOICE_USER_END
    schedule.assert_called_once_with("test", session, voice_turn)


@pytest.mark.asyncio
async def test_non_barge_turn_complete_uses_shared_handoff(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(turn_id="turn-normal", transcript_text="hello there")
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_started_at=0.0,
        barge_in_candidate_committed=False,
        endpoint_decision_task=None,
        processor=SimpleNamespace(turn_phase=SpeechTurnPhase.ENDPOINT_CANDIDATE),
    )
    publish = AsyncMock()
    schedule = MagicMock()
    monkeypatch.setattr(handlers, "event_bus", SimpleNamespace(publish=publish))
    monkeypatch.setattr(handlers, "_schedule_endpoint_decision", schedule)

    await handlers._handoff_endpointed_voice_turn("test", session, voice_turn)

    publish.assert_awaited_once()
    assert publish.await_args.args[0].type is EventType.VOICE_USER_END
    schedule.assert_called_once_with("test", session, voice_turn)

@pytest.mark.asyncio
async def test_push_to_talk_barge_commit_skips_endpoint_scheduler(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-ptt",
        transcript_text="actually tell me the next one",
    )
    processor = SimpleNamespace(
        request_turn_commit=MagicMock(return_value=True),
        turn_phase=SpeechTurnPhase.ENDPOINT_CANDIDATE,
        peek_turn_speech_audio=MagicMock(return_value=b""),
    )
    session = SimpleNamespace(
        processor=processor,
        voice_turn=voice_turn,
        barge_in_candidate_turn=voice_turn,
        stt_stream=None,
        endpoint_decision_task=None,
        accepted_input_task=None,
        barge_in_candidate_started_at=time.monotonic() - 0.3,
        barge_in_candidate_committed=False,
        barge_in_candidate_task=None,
        barge_in_speaker_task=None,
        barge_in_speaker_evidence=None,
        barge_in_speaker_attempts=0,
        speaker_verifier=None,
        current_trigger_instance_id=None,
    )
    schedule = MagicMock()
    commit = AsyncMock()
    publish = AsyncMock()
    send_message = AsyncMock()
    send_voice_response = AsyncMock()
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(
            get_session=lambda _sid: session,
            send_message=send_message,
            send_voice_response=send_voice_response,
        ),
    )
    monkeypatch.setattr(handlers, "_schedule_endpoint_decision", schedule)
    monkeypatch.setattr(handlers, "_commit_voice_turn", commit)
    monkeypatch.setattr(handlers, "_cancel_endpoint_decision", MagicMock())
    monkeypatch.setattr(
        handlers,
        "_sync_voice_turn_transcript",
        lambda *a, **k: voice_turn.transcript_text,
    )
    monkeypatch.setattr(handlers, "_publish_voice_user_start", AsyncMock())
    monkeypatch.setattr(handlers, "event_bus", SimpleNamespace(publish=publish))

    await handlers.handle_voice_commit(
        "test",
        WSMessage(type=WSMessageType.VOICE_COMMIT, data={}),
    )

    assert session.barge_in_candidate_committed is True
    schedule.assert_not_called()
    commit.assert_awaited_once()
    decision = commit.await_args.args[3]
    assert isinstance(decision, TurnDecision)
    assert decision.reason == "push_to_talk_release"
    assert voice_turn.admission_source == "barge_in"
    assert voice_turn.admission_reason == "endpoint_has_text"
    assert publish.await_count == 1
    assert publish.await_args.args[0].type is EventType.VOICE_USER_END


@pytest.mark.asyncio
async def test_handler_commit_stamps_barge_in_admission(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-1",
        transcript_text="actually tell me the next one",
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_turn=voice_turn,
        stt_stream=None,
        current_trigger_instance_id=None,
        barge_in_candidate_started_at=time.monotonic() - 0.3,
        barge_in_candidate_task=None,
        barge_in_candidate_committed=False,
        barge_in_speaker_task=None,
        barge_in_speaker_evidence=None,
        barge_in_speaker_attempts=0,
        speaker_verifier=None,
        processor=SimpleNamespace(peek_turn_speech_audio=MagicMock(return_value=b"")),
    )
    monkeypatch.setattr(handlers, "_publish_voice_user_start", AsyncMock())
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=AsyncMock()),
    )

    decision = await handlers._resolve_barge_in_candidate("test", session, endpointed=True)

    assert decision is AdmissionAction.COMMIT
    assert voice_turn.admission_source == "barge_in"
    assert voice_turn.admission_reason == "endpoint_has_text"


@pytest.mark.asyncio
async def test_commit_voice_turn_followup_fail_open_schedules_process_turn(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-follow",
        transcript_text="what time is it",
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_committed=False,
        soft_muted=False,
        stt_stream=None,
        current_run_task=None,
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"\x00\x00" * 160),
            mode=handlers.VoiceMode.ACTIVE_IDLE,
            force_active=MagicMock(),
        ),
        pending_attachments=[],
    )
    process_turn = AsyncMock()
    monkeypatch.setattr(handlers, "_flush_turn_detector", MagicMock())
    monkeypatch.setattr(
        handlers,
        "_finish_streaming_stt",
        AsyncMock(return_value=("what time is it", {"bytes_fed": 320, "feed_count": 1})),
    )
    monkeypatch.setattr(handlers, "_handle_local_voice_command", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers.orchestrator, "process_turn", process_turn)
    monkeypatch.setattr(handlers, "manager", SimpleNamespace(send_voice_response=AsyncMock()))

    await handlers._commit_voice_turn(
        "test",
        session,
        voice_turn,
        TurnDecision(done=True, reason="audio_eou"),
    )
    if session.current_run_task is not None:
        await session.current_run_task

    assert voice_turn.admission_source == "followup"
    assert voice_turn.admission_reason == "followup_open"
    process_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_voice_turn_reuses_barge_in_admission(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-barge",
        transcript_text="stop that",
        admission_source="barge_in",
        admission_reason="owner_endpoint",
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_committed=True,
        soft_muted=False,
        stt_stream=None,
        current_run_task=None,
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"\x00\x00" * 160),
        ),
        pending_attachments=[],
        barge_in_candidate_task=None,
        barge_in_speaker_task=None,
        barge_in_speaker_evidence=None,
        barge_in_speaker_attempts=0,
        barge_in_candidate_turn=None,
        barge_in_candidate_started_at=1.0,
    )
    followup = MagicMock(
        return_value=handlers.AdmissionDecision(
            handlers.AdmissionAction.SUPPRESS, "should_not_run"
        )
    )
    process_turn = AsyncMock()
    monkeypatch.setattr(handlers, "_flush_turn_detector", MagicMock())
    monkeypatch.setattr(
        handlers,
        "_finish_streaming_stt",
        AsyncMock(return_value=("stop that", {"bytes_fed": 320, "feed_count": 1})),
    )
    monkeypatch.setattr(handlers, "_handle_local_voice_command", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers, "decide_followup_admission", followup)
    monkeypatch.setattr(handlers.orchestrator, "process_turn", process_turn)
    monkeypatch.setattr(handlers, "manager", SimpleNamespace(send_voice_response=AsyncMock()))

    await handlers._commit_voice_turn(
        "test",
        session,
        voice_turn,
        TurnDecision(done=True, reason="audio_eou"),
    )
    if session.current_run_task is not None:
        await session.current_run_task

    followup.assert_not_called()
    assert voice_turn.admission_source == "barge_in"
    assert voice_turn.admission_reason == "owner_endpoint"
    process_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_commit_voice_turn_wake_bypasses_followup_policy(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-wake",
        transcript_text="set a timer",
        from_wake=True,
        admission_source="wake",
        admission_reason="wake_word",
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_committed=False,
        soft_muted=False,
        stt_stream=None,
        current_run_task=None,
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"\x00\x00" * 160),
        ),
        pending_attachments=[],
    )
    followup = MagicMock()
    process_turn = AsyncMock()
    monkeypatch.setattr(handlers, "_flush_turn_detector", MagicMock())
    monkeypatch.setattr(
        handlers,
        "_finish_streaming_stt",
        AsyncMock(return_value=("set a timer", {"bytes_fed": 320, "feed_count": 1})),
    )
    monkeypatch.setattr(handlers, "_handle_local_voice_command", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers, "decide_followup_admission", followup)
    monkeypatch.setattr(handlers.orchestrator, "process_turn", process_turn)
    monkeypatch.setattr(handlers, "manager", SimpleNamespace(send_voice_response=AsyncMock()))

    await handlers._commit_voice_turn(
        "test",
        session,
        voice_turn,
        TurnDecision(done=True, reason="audio_eou"),
    )
    if session.current_run_task is not None:
        await session.current_run_task

    followup.assert_not_called()
    assert voice_turn.admission_source == "wake"
    process_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_suppress_finalized_followup_retracts_and_skips_process_turn(monkeypatch) -> None:
    voice_turn = VoiceInputTurn(
        turn_id="turn-suppress",
        transcript_text="side chat",
    )
    session = SimpleNamespace(
        voice_turn=voice_turn,
        barge_in_candidate_committed=False,
        soft_muted=False,
        stt_stream=None,
        current_run_task=None,
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"\x00\x00" * 160),
            mode=handlers.VoiceMode.ACTIVE_IDLE,
            force_active=MagicMock(),
        ),
        pending_attachments=[],
    )
    send_message = AsyncMock()
    send_voice_response = AsyncMock()
    process_turn = AsyncMock()
    monkeypatch.setattr(handlers, "_flush_turn_detector", MagicMock())
    monkeypatch.setattr(
        handlers,
        "_finish_streaming_stt",
        AsyncMock(return_value=("side chat", {"bytes_fed": 320, "feed_count": 1})),
    )
    monkeypatch.setattr(handlers, "_handle_local_voice_command", AsyncMock(return_value=False))
    monkeypatch.setattr(
        handlers,
        "decide_followup_admission",
        MagicMock(
            return_value=handlers.AdmissionDecision(
                handlers.AdmissionAction.SUPPRESS, "not_directed"
            )
        ),
    )
    monkeypatch.setattr(handlers, "_close_streaming_stt", AsyncMock())
    monkeypatch.setattr(handlers, "_close_turn_detector", AsyncMock())
    monkeypatch.setattr(handlers, "_discard_voice_turn_latency", MagicMock())
    monkeypatch.setattr(handlers.orchestrator, "process_turn", process_turn)
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_message=send_message, send_voice_response=send_voice_response),
    )

    await handlers._commit_voice_turn(
        "test",
        session,
        voice_turn,
        TurnDecision(done=True, reason="audio_eou"),
    )

    process_turn.assert_not_called()
    send_message.assert_awaited_once()
    assert send_message.await_args.args[1].type is WSMessageType.RETRACT
    send_voice_response.assert_awaited()
    session.processor.force_active.assert_called_once()
