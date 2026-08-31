import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.turns.orchestrator import AssistantOrchestrator
from core.attention.models import AttentionState
from core.voice.processor import VoiceMode
from core.voice.turn_detector import TurnDecision
from services.events import Event, EventType
from api.websockets.connection import VoiceInputTurn
from api.websockets.models import WSMessage
from api.websockets.presence import LocationRef, PresenceIdentity
from api.websockets import handlers
from api.websockets.types import WSMessageType


def _manager_stub(session=None, **extras):
    return SimpleNamespace(
        get_session=MagicMock(return_value=session),
        get_session_by_connection=MagicMock(return_value=session),
        record_user_turn_activity=MagicMock(),
        **extras,
    )


@pytest.mark.asyncio
async def test_playback_end_ignores_stale_turn_id() -> None:
    processor = SimpleNamespace(
        mode=VoiceMode.ACTIVE_AI_TURN,
        set_mode=MagicMock(),
        refresh_activity=MagicMock(),
    )
    session = SimpleNamespace(
        owner_id="owner-1",
        connection_id="conn-1",
        presence=SimpleNamespace(node_id="node-1"),
        current_run_task=None,
        active_audio_turn_id="turn-new",
        first_audio_sent=False,
        last_turn_audio_sent=True,
        soft_muted=False,
        processor=processor,
    )
    fake_manager = _manager_stub(session, send_message=AsyncMock())

    with patch.object(handlers, "manager", fake_manager):
        await handlers.handle_playback_end(
            "test",
            WSMessage(
                id="playback-old",
                type=WSMessageType.PLAYBACK_END,
                data={"turn_id": "turn-old"},
            ),
        )

    processor.set_mode.assert_not_called()
    processor.refresh_activity.assert_not_called()
    assert session.active_audio_turn_id == "turn-new"
    fake_manager.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_playback_end_matching_turn_id_resets_active_ai_turn() -> None:
    class Processor:
        def __init__(self) -> None:
            self.mode = VoiceMode.ACTIVE_AI_TURN
            self.refresh_activity = MagicMock()

        def set_mode(self, mode: VoiceMode, *, source: str) -> None:
            self.mode = mode
            self.set_mode_call = (mode, source)

    processor = Processor()
    session = SimpleNamespace(
        owner_id="owner-1",
        connection_id="conn-1",
        presence=SimpleNamespace(node_id="node-1"),
        current_run_task=None,
        active_audio_turn_id="turn-current",
        first_audio_sent=False,
        last_turn_audio_sent=True,
        soft_muted=False,
        processor=processor,
    )
    fake_manager = _manager_stub(session, send_message=AsyncMock())

    with patch.object(handlers, "manager", fake_manager):
        await handlers.handle_playback_end(
            "test",
            WSMessage(
                id="playback-current",
                type=WSMessageType.PLAYBACK_END,
                data={"turn_id": "turn-current"},
            ),
        )

    assert processor.set_mode_call == (VoiceMode.ACTIVE_IDLE, "ws.audio_playback_end")
    processor.refresh_activity.assert_called_once_with(source="playback_end")
    assert session.active_audio_turn_id is None
    response = fake_manager.send_message.await_args.args[1]
    assert response.data == {"stage": "listening"}


@pytest.mark.asyncio
async def test_system_turn_does_not_clear_active_user_voice_turn() -> None:
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    voice_turn = SimpleNamespace(turn_id="user-turn")
    session = SimpleNamespace(
        context={},
        owner_id="test-system",
        connection_id="test-system",
        turn_lock=asyncio.Lock(),
        voice_turn=voice_turn,
        accepted_input_task=None,
        current_run_task=None,
        processor=SimpleNamespace(
            mode=VoiceMode.ACTIVE_IDLE,
            set_mode=MagicMock(),
        ),
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock())

    with patch("api.websockets.connection.manager", fake_manager):
        await orchestrator.process_turn(
            connection_id="test-system",
            audio_bytes=None,
            system_context="SYSTEM EVENT: test",
            source="system",
            turn_id="system-turn",
        )

    assert session.voice_turn is voice_turn
    assert session.voice_turn.turn_id == "user-turn"


@pytest.mark.asyncio
async def test_persist_trace_skips_prepersisted_user_row() -> None:
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    trace = [
        ("user", "hello"),
        ("assistant", "hi", {"turn_type": "text_only"}),
    ]

    with patch("core.turns.orchestrator.mongodb") as mock_db:
        mock_db.store_message = AsyncMock()

        await orchestrator._persist_trace(
            "test",
            "user",
            trace,
            turn_id="turn-1",
            skip_initial_user=True,
        )

    mock_db.store_message.assert_awaited_once_with(
        "test",
        "assistant",
        "hi",
        source="user",
        metadata={"turn_type": "text_only", "turn_id": "turn-1"},
    )


@pytest.mark.asyncio
async def test_persist_trace_uses_explicit_presence_metadata() -> None:
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    trace = [("assistant", "hi", {"turn_type": "text_only"})]
    presence_metadata = {
        "owner_id": "geoff",
        "connection_id": "conn-original",
        "node_id": "office-node",
        "location_ref": {"provider": "manual", "room_id": "office"},
    }

    with patch("core.turns.orchestrator.mongodb") as mock_db:
        mock_db.store_message = AsyncMock()

        await orchestrator._persist_trace(
            "geoff",
            "system",
            trace,
            turn_id="turn-1",
            presence_metadata=presence_metadata,
        )

    mock_db.store_message.assert_awaited_once_with(
        "geoff",
        "assistant",
        "hi",
        source="system",
        metadata={
            "turn_type": "text_only",
            "owner_id": "geoff",
            "connection_id": "conn-original",
            "node_id": "office-node",
            "location_ref": {"provider": "manual", "room_id": "office"},
            "turn_id": "turn-1",
        },
    )


@pytest.mark.asyncio
async def test_user_turn_is_upserted_pending_then_marked_completed() -> None:
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    orchestrator.tts = MagicMock()
    session = SimpleNamespace(
        context={},
        owner_id="test",
        connection_id="test",
        presence=PresenceIdentity(
            connection_id="test",
            owner_id="test",
            node_id="kitchen-sat",
            node_label="Kitchen",
            capabilities=frozenset({"mic", "speaker"}),
            device_kind="satellite",
            location=LocationRef.from_values(room_name="Kitchen"),
        ),
        turn_lock=asyncio.Lock(),
        voice_turn=None,
        accepted_input_task=None,
        current_run_task=None,
        processor=SimpleNamespace(
            mode=VoiceMode.ACTIVE_IDLE,
            set_mode=MagicMock(),
        ),
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock())

    async def fake_execute_turn(*args, result, **kwargs):
        result.turn_trace.append(("user", "hello"))
        result.turn_trace.append(("assistant", "hi", {"turn_type": "text_only"}))
        result.full_response = "hi"

    with (
        patch("api.websockets.connection.manager", fake_manager),
        patch("core.turns.orchestrator.mongodb") as mock_db,
        patch.object(orchestrator, "_execute_turn", new=AsyncMock(side_effect=fake_execute_turn)),
    ):
        mock_db.upsert_user_turn = AsyncMock()
        mock_db.mark_user_turn_status = AsyncMock()
        mock_db.store_message = AsyncMock()

        await orchestrator.process_turn(
            connection_id="test",
            audio_bytes=None,
            text="hello",
            turn_id="turn-1",
        )

    mock_db.upsert_user_turn.assert_awaited_once()
    upsert_call = mock_db.upsert_user_turn.await_args
    assert upsert_call.args[:3] == ("test", "turn-1", "hello")
    meta = upsert_call.kwargs.get("metadata") or upsert_call.args[3] if len(upsert_call.args) > 3 else upsert_call.kwargs["metadata"]
    assert meta["turn_status"] == "pending"
    assert meta["node_id"] == "kitchen-sat"
    assert meta["node_label"] == "Kitchen"
    mock_db.mark_user_turn_status.assert_awaited_once_with(
        "test", "turn-1", "completed", delivery=None,
    )
    mock_db.store_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_turn_no_reply_suppressed() -> None:
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    orchestrator.tts = MagicMock()

    class Processor:
        def __init__(self) -> None:
            self.mode = VoiceMode.ACTIVE_IDLE
            self.set_mode = MagicMock()
            self.refresh_activity = MagicMock()
            self.force_passive = MagicMock()

    processor = Processor()
    session = SimpleNamespace(
        context={},
        turn_lock=asyncio.Lock(),
        voice_turn=None,
        accepted_input_task=None,
        current_run_task=None,
        owner_id="test",
        connection_id="test",
        soft_muted=False,
        active_audio_turn_id=None,
        processor=processor,
        last_turn_routed_tools=set(),
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock(return_value="msg-id"))

    voice = AsyncMock()
    voice.first_audio_sent = False
    voice.response_id = "resp-no-reply"
    voice.start = AsyncMock()
    voice.aclose = AsyncMock()
    voice.send_tts_end_if_ready = AsyncMock()

    async def fake_execute_turn(*args, result, **kwargs):
        result.turn_trace.append(
            ("assistant", "NO_REPLY", {"turn_type": "text_only"}),
        )
        result.full_response = "NO_REPLY"
        result.delivered_text = ""

    with (
        patch("api.websockets.connection.manager", fake_manager),
        patch("core.turns.orchestrator.mongodb") as mock_db,
        patch("core.turns.orchestrator.VoiceDelivery", return_value=voice),
        patch.object(orchestrator, "_execute_turn", new=AsyncMock(side_effect=fake_execute_turn)),
        patch("core.turns.orchestrator.event_bus.publish", new=AsyncMock()),
    ):
        mock_db.upsert_user_turn = AsyncMock()
        mock_db.mark_user_turn_status = AsyncMock()
        mock_db.store_message = AsyncMock()

        await orchestrator.process_turn(
            connection_id="test",
            audio_bytes=b"\x00\x00",
            text="so then she told him to leave",
            turn_id="turn-no-reply",
        )

    mock_db.mark_user_turn_status.assert_awaited_once_with(
        "test",
        "turn-no-reply",
        "completed",
        delivery="suppressed",
    )
    assert mock_db.store_message.await_count == 1
    assert mock_db.store_message.await_args.kwargs["metadata"]["delivery"] == "suppressed"
    response_msgs = [
        c
        for c in fake_manager.send_voice_response.await_args_list
        if len(c.args) >= 2 and c.args[1] == WSMessageType.RESPONSE
    ]
    assert response_msgs == []
    no_reply_msgs = [
        c
        for c in fake_manager.send_voice_response.await_args_list
        if len(c.args) >= 2 and c.args[1] == WSMessageType.NO_REPLY
    ]
    assert len(no_reply_msgs) == 1
    assert no_reply_msgs[0].args[2] == {"text": "Jarvis didn't reply."}
    assert no_reply_msgs[0].kwargs["message_id"] == "turn-no-reply"
    status_msgs = [
        c
        for c in fake_manager.send_voice_response.await_args_list
        if len(c.args) >= 2 and c.args[1] == WSMessageType.STATUS and c.args[2].get("stage") in {"idle", "listening"}
    ]
    assert status_msgs[-1].args[2] == {"stage": "idle"}
    processor.force_passive.assert_called_once_with(
        reason="orchestrator.user_no_reply",
        release_wake_refractory=True,
        arm_post_tts_suppression=False,
    )
    processor.set_mode.assert_any_call(VoiceMode.ACTIVE_AI_TURN, source="orchestrator.turn_start")
    idle_resets = [
        call
        for call in processor.set_mode.call_args_list
        if call.args and call.args[0] == VoiceMode.ACTIVE_IDLE
        and call.kwargs.get("source") == "orchestrator.turn_finally_no_audio"
    ]
    assert idle_resets == []
    processor.refresh_activity.assert_not_called()


@pytest.mark.asyncio
async def test_voice_transcript_payload_uses_turn_identity_without_voice_turn() -> None:
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    orchestrator.tts = MagicMock()
    sent: list[tuple[str, WSMessageType, dict, str | None]] = []

    async def send_voice_response(
        connection_id: str,
        message_type: WSMessageType,
        data: dict,
        *,
        message_id: str | None = None,
    ) -> str:
        sent.append((connection_id, message_type, data, message_id))
        return message_id or "generated-message"

    session = SimpleNamespace(
        context={},
        turn_lock=asyncio.Lock(),
        voice_turn=None,
        accepted_input_task=None,
        current_run_task=None,
        current_delivery=None,
        tts_sentence_queue=None,
        first_audio_sent=False,
        last_turn_audio_sent=False,
        soft_muted=False,
        owner_id="test",
        connection_id="test",
        processor=SimpleNamespace(
            mode=VoiceMode.ACTIVE_IDLE,
            set_mode=MagicMock(),
            refresh_activity=MagicMock(),
        ),
    )
    fake_manager = _manager_stub(session, send_voice_response=send_voice_response)

    async def fake_execute_turn(*args, result, **kwargs):
        result.turn_trace.append(("user", "hello"))
        result.turn_trace.append(("assistant", "hi", {"turn_type": "text_only"}))
        result.full_response = "hi"

    with (
        patch("api.websockets.connection.manager", fake_manager),
        patch("core.turns.orchestrator.mongodb") as mock_db,
        patch.object(orchestrator, "_execute_turn", new=AsyncMock(side_effect=fake_execute_turn)),
    ):
        mock_db.upsert_user_turn = AsyncMock()
        mock_db.mark_user_turn_status = AsyncMock()
        mock_db.store_message = AsyncMock()

        await orchestrator.process_turn(
            connection_id="test",
            audio_bytes=b"pcm",
            text="hello",
            turn_id="turn-voice",
        )

    transcript = next(item for item in sent if item[1] == WSMessageType.TRANSCRIPT)
    assert transcript[2] == {
        "text": "hello",
        "turn_id": "turn-voice",
    }
    assert transcript[3] == "turn-voice"


@pytest.mark.asyncio
async def test_partial_transcript_payload_includes_turn_identity(monkeypatch) -> None:
    send_voice_response = AsyncMock(return_value="turn-voice")
    monkeypatch.setattr(
        handlers,
        "manager",
        SimpleNamespace(send_voice_response=send_voice_response),
    )
    voice_turn = VoiceInputTurn(turn_id="turn-voice")

    await handlers._send_partial_transcript("test", voice_turn, "hello")

    send_voice_response.assert_awaited_once_with(
        "test",
        WSMessageType.PARTIAL_TRANSCRIPT,
        {
            "text": "hello",
            "turn_id": "turn-voice",
        },
        message_id="turn-voice",
    )


@pytest.mark.asyncio
async def test_voice_user_start_without_active_task_keeps_voice_turn() -> None:
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    voice_turn = SimpleNamespace(turn_id="user-turn")
    session = SimpleNamespace(
        voice_turn=voice_turn,
        accepted_input_task=None,
        current_run_task=None,
    )
    fake_manager = _manager_stub(session)

    with patch("api.websockets.connection.manager", fake_manager):
        await orchestrator._handle_interruption(
            Event(
                type=EventType.VOICE_USER_START,
                source="test",
                data={"session_id": "test"},
            )
        )

    assert session.voice_turn is voice_turn
    assert session.voice_turn.turn_id == "user-turn"


@pytest.mark.asyncio
async def test_voice_user_start_cancels_run_without_clearing_voice_turn() -> None:
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    voice_turn = SimpleNamespace(turn_id="user-turn")
    active_task = MagicMock()
    active_task.done.return_value = False
    active_task.cancel = MagicMock()
    session = SimpleNamespace(
        voice_turn=voice_turn,
        soft_muted=False,
        accepted_input_task=None,
        current_run_task=active_task,
        current_delivery=None,
        tts_sentence_queue=None,
        processor=SimpleNamespace(
            mode=VoiceMode.ACTIVE_AI_TURN,
            set_mode=MagicMock(),
        ),
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock(return_value="stop-msg"))

    with patch("api.websockets.connection.manager", fake_manager):
        await orchestrator._handle_interruption(
            Event(
                type=EventType.VOICE_USER_START,
                source="test",
                data={"session_id": "test"},
            )
        )

    active_task.cancel.assert_called_once()
    assert session.voice_turn is voice_turn
    assert session.voice_turn.turn_id == "user-turn"
    fake_manager.send_voice_response.assert_awaited_once_with(
        "test",
        WSMessageType.STOP,
        {"reason": "interruption"},
    )


@pytest.mark.asyncio
async def test_voice_user_start_stop_payload_includes_delivery_ids() -> None:
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    active_task = MagicMock()
    active_task.done.return_value = False
    active_task.cancel = MagicMock()
    current_delivery = SimpleNamespace(
        turn_id="turn-active",
        response_id="response-active",
        signal_cancel=MagicMock(),
    )
    session = SimpleNamespace(
        voice_turn=SimpleNamespace(turn_id="user-turn"),
        soft_muted=False,
        accepted_input_task=None,
        current_run_task=active_task,
        current_delivery=current_delivery,
        tts_sentence_queue=None,
        processor=SimpleNamespace(
            mode=VoiceMode.ACTIVE_AI_TURN,
            set_mode=MagicMock(),
        ),
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock(return_value="stop-msg"))

    with patch("api.websockets.connection.manager", fake_manager):
        await orchestrator._handle_interruption(
            Event(
                type=EventType.VOICE_USER_START,
                source="test",
                data={"session_id": "test"},
            )
        )

    current_delivery.signal_cancel.assert_called_once_with()
    active_task.cancel.assert_called_once()
    fake_manager.send_voice_response.assert_awaited_once_with(
        "test",
        WSMessageType.STOP,
        {
            "reason": "interruption",
            "response_id": "response-active",
            "turn_id": "turn-active",
        },
    )


@pytest.mark.asyncio
async def test_voice_user_start_while_soft_muted_does_not_cancel_active_task() -> None:
    """VOICE_USER_START must not barge into a proactive notification turn while muted."""
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    active_task = MagicMock()
    active_task.done.return_value = False
    active_task.cancel = MagicMock()

    session = SimpleNamespace(
        voice_turn=None,
        soft_muted=True,
        accepted_input_task=None,
        current_run_task=active_task,
    )
    fake_manager = _manager_stub(session)

    with patch("api.websockets.connection.manager", fake_manager):
        await orchestrator._handle_interruption(
            Event(
                type=EventType.VOICE_USER_START,
                source="test",
                data={"session_id": "test"},
            )
        )

    active_task.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_voice_wake_while_soft_muted_still_interrupts_active_task() -> None:
    """Wake word should still barge in even while soft_muted."""
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    active_task = MagicMock()
    active_task.done.return_value = False
    active_task.cancel = MagicMock()

    session = SimpleNamespace(
        voice_turn=None,
        soft_muted=True,
        accepted_input_task=None,
        current_run_task=active_task,
        tts_sentence_queue=None,
        processor=SimpleNamespace(
            mode="active_ai_turn",
            set_mode=MagicMock(),
        ),
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock())

    with patch("api.websockets.connection.manager", fake_manager):
        await orchestrator._handle_interruption(
            Event(
                type=EventType.VOICE_WAKE,
                source="test",
                data={"session_id": "test"},
            )
        )

    active_task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_stop_acknowledges_active_ackable_trigger() -> None:
    session = SimpleNamespace(
        processor=SimpleNamespace(
            mode=VoiceMode.ACTIVE_AI_TURN,
            force_passive=MagicMock(),
        ),
        current_run_task=None,
        current_trigger_instance_id="trg-active",
    )
    trigger_service = SimpleNamespace(
        acknowledge_instance=AsyncMock(return_value=True),
        cancel_instance=AsyncMock(),
    )
    fake_manager = SimpleNamespace(
        get_session=MagicMock(return_value=session),
        send_message=AsyncMock(),
    )

    with (
        patch.object(handlers, "manager", fake_manager),
        patch.object(handlers, "_cancel_endpoint_decision", MagicMock()),
        patch.object(handlers, "_close_streaming_stt", new=AsyncMock()),
        patch.object(handlers, "_discard_voice_turn_latency", MagicMock()),
        patch.object(handlers.event_bus, "publish", new=AsyncMock()),
        patch("core.triggers.service.trigger_service", trigger_service),
    ):
        await handlers.handle_stop_signal(
            "conn-1",
            handlers.WSMessage(type=handlers.WSMessageType.STOP),
        )

    trigger_service.acknowledge_instance.assert_awaited_once_with("trg-active")
    trigger_service.cancel_instance.assert_not_awaited()
    session.processor.force_passive.assert_called_once_with(reason="ws.system_stop")


@pytest.mark.asyncio
async def test_stop_cancels_active_non_ackable_trigger() -> None:
    session = SimpleNamespace(
        processor=SimpleNamespace(
            mode=VoiceMode.ACTIVE_AI_TURN,
            force_passive=MagicMock(),
        ),
        current_run_task=None,
        current_trigger_instance_id="trg-active",
    )
    trigger_service = SimpleNamespace(
        acknowledge_instance=AsyncMock(return_value=False),
        cancel_instance=AsyncMock(),
    )
    fake_manager = SimpleNamespace(
        get_session=MagicMock(return_value=session),
        send_message=AsyncMock(),
    )

    with (
        patch.object(handlers, "manager", fake_manager),
        patch.object(handlers, "_cancel_endpoint_decision", MagicMock()),
        patch.object(handlers, "_close_streaming_stt", new=AsyncMock()),
        patch.object(handlers, "_discard_voice_turn_latency", MagicMock()),
        patch.object(handlers.event_bus, "publish", new=AsyncMock()),
        patch("core.triggers.service.trigger_service", trigger_service),
    ):
        await handlers.handle_stop_signal(
            "conn-1",
            handlers.WSMessage(type=handlers.WSMessageType.STOP),
        )

    trigger_service.acknowledge_instance.assert_awaited_once_with("trg-active")
    trigger_service.cancel_instance.assert_awaited_once_with("trg-active")


@pytest.mark.asyncio
async def test_local_soft_mute_command_does_not_schedule_process_turn() -> None:
    voice_turn = VoiceInputTurn(turn_id="turn-local", transcript_text="Jarvis mute")
    session = SimpleNamespace(
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"audio"),
            force_passive=MagicMock(),
            force_active=MagicMock(),
        ),
        stt_stream=None,
        voice_turn=voice_turn,
        soft_muted=False,
    )
    fake_manager = SimpleNamespace(send_voice_response=AsyncMock())

    with (
        patch.object(handlers, "manager", fake_manager),
        patch.object(handlers.orchestrator, "_deliver_text", new=AsyncMock()) as deliver_text,
        patch.object(handlers.orchestrator, "process_turn", new=AsyncMock()) as process_turn,
    ):
        await handlers._commit_voice_turn(
            "conn-1",
            session,
            voice_turn,
            TurnDecision(done=True, reason="test"),
        )

    assert session.soft_muted is True
    session.processor.force_passive.assert_called_once_with(reason="local_command.soft_mute")
    assert fake_manager.send_voice_response.await_count == 2
    fake_manager.send_voice_response.assert_any_await(
        "conn-1", handlers.WSMessageType.STOP, {"reason": "local_mute"}
    )
    fake_manager.send_voice_response.assert_any_await(
        "conn-1",
        handlers.WSMessageType.STATUS,
        {"stage": "idle", "session": {"soft_muted": True}},
    )
    process_turn.assert_not_called()


@pytest.mark.asyncio
async def test_soft_muted_session_drops_non_unmute_transcript() -> None:
    voice_turn = VoiceInputTurn(turn_id="turn-drop", transcript_text="Jarvis turn on the lights")
    session = SimpleNamespace(
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"audio"),
            force_passive=MagicMock(),
            force_active=MagicMock(),
        ),
        stt_stream=None,
        voice_turn=voice_turn,
        soft_muted=True,
    )
    fake_manager = SimpleNamespace(send_voice_response=AsyncMock())

    with (
        patch.object(handlers, "manager", fake_manager),
        patch.object(handlers.orchestrator, "_deliver_text", new=AsyncMock()) as deliver_text,
        patch.object(handlers.orchestrator, "process_turn", new=AsyncMock()) as process_turn,
    ):
        await handlers._commit_voice_turn(
            "conn-1",
            session,
            voice_turn,
            TurnDecision(done=True, reason="test"),
        )

    assert session.soft_muted is True
    session.processor.force_passive.assert_called_once_with(reason="local_command.drop")
    process_turn.assert_not_called()


@pytest.mark.asyncio
async def test_soft_muted_session_accepts_unmute_without_process_turn() -> None:
    voice_turn = VoiceInputTurn(turn_id="turn-unmute", transcript_text="Jarvis unmute")
    session = SimpleNamespace(
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"audio"),
            force_passive=MagicMock(),
            force_active=MagicMock(),
        ),
        stt_stream=None,
        voice_turn=voice_turn,
        soft_muted=True,
    )
    fake_manager = SimpleNamespace(send_voice_response=AsyncMock())

    with (
        patch.object(handlers, "manager", fake_manager),
        patch.object(handlers.orchestrator, "_deliver_text", new=AsyncMock()) as deliver_text,
        patch.object(handlers.orchestrator, "process_turn", new=AsyncMock()) as process_turn,
    ):
        await handlers._commit_voice_turn(
            "conn-1",
            session,
            voice_turn,
            TurnDecision(done=True, reason="test"),
        )

    assert session.soft_muted is False
    assert session.voice_turn is None
    session.processor.force_active.assert_called_once_with(reason="local_command.unmute")
    fake_manager.send_voice_response.assert_awaited_once_with(
        "conn-1",
        handlers.WSMessageType.STATUS,
        {"session": {"soft_muted": False}},
    )
    deliver_text.assert_awaited_once_with(
        "conn-1",
        "Online.",
        None,
        delivery="local_command",
        persist=False,
    )
    process_turn.assert_not_called()


@pytest.mark.asyncio
async def test_power_down_enters_paused_soft_mute_without_process_turn() -> None:
    voice_turn = VoiceInputTurn(turn_id="turn-power-down", transcript_text="Jarvis power down")
    session = SimpleNamespace(
        owner_id="owner-1",
        presence=SimpleNamespace(node_id="node-1"),
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"audio"),
            force_passive=MagicMock(),
            force_active=MagicMock(),
        ),
        stt_stream=None,
        voice_turn=voice_turn,
        soft_muted=False,
    )
    fake_manager = SimpleNamespace(send_voice_response=AsyncMock())

    with (
        patch.object(handlers, "manager", fake_manager),
        patch.object(
            handlers,
            "_set_attention_mode_fast_path",
            new=AsyncMock(return_value=AttentionState(owner_id="owner-1", mode="paused")),
        ) as set_attention,
        patch.object(handlers.orchestrator, "_deliver_text", new=AsyncMock()) as deliver_text,
        patch.object(handlers.orchestrator, "process_turn", new=AsyncMock()) as process_turn,
    ):
        await handlers._commit_voice_turn(
            "conn-1",
            session,
            voice_turn,
            TurnDecision(done=True, reason="test"),
        )

    set_attention.assert_awaited_once_with("owner-1", "node-1", "paused")
    assert session.soft_muted is True
    session.processor.force_passive.assert_called_once_with(reason="local_command.power_down")
    fake_manager.send_voice_response.assert_any_await(
        "conn-1", handlers.WSMessageType.STOP, {"reason": "local_power_down"}
    )
    deliver_text.assert_not_awaited()
    process_turn.assert_not_called()


@pytest.mark.asyncio
async def test_soft_muted_session_accepts_power_on_without_process_turn() -> None:
    voice_turn = VoiceInputTurn(turn_id="turn-power-on", transcript_text="Jarvis power on")
    session = SimpleNamespace(
        owner_id="owner-1",
        presence=SimpleNamespace(node_id="node-1"),
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"audio"),
            force_passive=MagicMock(),
            force_active=MagicMock(),
        ),
        stt_stream=None,
        voice_turn=voice_turn,
        soft_muted=True,
    )
    fake_manager = SimpleNamespace(send_voice_response=AsyncMock())

    with (
        patch.object(handlers, "manager", fake_manager),
        patch.object(
            handlers,
            "_set_attention_mode_fast_path",
            new=AsyncMock(return_value=AttentionState(owner_id="owner-1", mode="active")),
        ) as set_attention,
        patch.object(handlers.orchestrator, "_deliver_text", new=AsyncMock()) as deliver_text,
        patch.object(handlers.orchestrator, "process_turn", new=AsyncMock()) as process_turn,
    ):
        await handlers._commit_voice_turn(
            "conn-1",
            session,
            voice_turn,
            TurnDecision(done=True, reason="test"),
        )

    set_attention.assert_awaited_once_with("owner-1", "node-1", "active")
    assert session.soft_muted is False
    assert session.voice_turn is None
    session.processor.force_active.assert_called_once_with(reason="local_command.power_on")
    deliver_text.assert_awaited_once_with(
        "conn-1",
        "Online.",
        None,
        delivery="local_command",
        persist=False,
    )
    process_turn.assert_not_called()


@pytest.mark.asyncio
async def test_power_check_from_paused_uses_special_ack_without_process_turn() -> None:
    voice_turn = VoiceInputTurn(turn_id="turn-power-check", transcript_text="Jarvis, you in there?")
    session = SimpleNamespace(
        owner_id="owner-1",
        presence=SimpleNamespace(node_id="node-1"),
        processor=SimpleNamespace(
            consume_turn_audio=MagicMock(return_value=b"audio"),
            force_passive=MagicMock(),
            force_active=MagicMock(),
        ),
        stt_stream=None,
        voice_turn=voice_turn,
        soft_muted=True,
    )
    fake_manager = SimpleNamespace(send_voice_response=AsyncMock())

    with (
        patch.object(handlers, "manager", fake_manager),
        patch.object(handlers, "_get_attention_mode_fast_path", new=AsyncMock(return_value="paused")) as get_attention,
        patch.object(
            handlers,
            "_set_attention_mode_fast_path",
            new=AsyncMock(return_value=AttentionState(owner_id="owner-1", mode="active")),
        ) as set_attention,
        patch.object(handlers.orchestrator, "_deliver_text", new=AsyncMock()) as deliver_text,
        patch.object(handlers.orchestrator, "process_turn", new=AsyncMock()) as process_turn,
    ):
        await handlers._commit_voice_turn(
            "conn-1",
            session,
            voice_turn,
            TurnDecision(done=True, reason="test"),
        )

    get_attention.assert_awaited_once_with("owner-1")
    set_attention.assert_awaited_once_with("owner-1", "node-1", "active")
    assert session.soft_muted is False
    session.processor.force_active.assert_called_once_with(reason="local_command.power_check")
    deliver_text.assert_awaited_once_with(
        "conn-1",
        "For you sir, always.",
        None,
        delivery="local_command",
        persist=False,
    )
    process_turn.assert_not_called()


@pytest.mark.asyncio
async def test_power_check_while_not_powered_down_falls_through_to_normal_turn() -> None:
    voice_turn = VoiceInputTurn(turn_id="turn-power-check-active", transcript_text="Jarvis, you in there?")
    session = SimpleNamespace(
        owner_id="owner-1",
        presence=SimpleNamespace(node_id="node-1"),
        processor=SimpleNamespace(
            force_passive=MagicMock(),
            force_active=MagicMock(),
        ),
        voice_turn=voice_turn,
        soft_muted=False,
    )

    with (
        patch.object(handlers, "_get_attention_mode_fast_path", new=AsyncMock(return_value="active")) as get_attention,
        patch.object(handlers, "_set_attention_mode_fast_path", new=AsyncMock()) as set_attention,
    ):
        handled = await handlers._handle_local_voice_command("conn-1", session, voice_turn)

    assert handled is False
    get_attention.assert_awaited_once_with("owner-1")
    set_attention.assert_not_awaited()
    session.processor.force_active.assert_not_called()
    session.processor.force_passive.assert_not_called()


@pytest.mark.asyncio
async def test_system_turn_no_audio_releases_active_ai_turn() -> None:
    class Processor:
        def __init__(self) -> None:
            self.mode = VoiceMode.ACTIVE_IDLE
            self.refresh_activity = MagicMock()

        def set_mode(self, mode: VoiceMode, *, source: str) -> None:
            self.mode = mode
            self.last_source = source

        def force_passive(self, *, reason: str = "unspecified", release_wake_refractory: bool = False) -> None:
            self.mode = VoiceMode.PASSIVE

    processor = Processor()
    session = SimpleNamespace(
        context={},
        owner_id="owner-1",
        connection_id="conn-mode-no-audio",
        turn_lock=asyncio.Lock(),
        voice_turn=None,
        accepted_input_task=None,
        current_run_task=None,
        active_audio_turn_id=None,
        soft_muted=False,
        processor=processor,
        last_turn_routed_tools=set(),
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock())
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    orchestrator.tts = MagicMock()

    voice = AsyncMock()
    voice.first_audio_sent = False
    voice.response_id = "resp-1"
    voice.start = AsyncMock()
    voice.aclose = AsyncMock()
    voice.send_tts_end_if_ready = AsyncMock()

    with (
        patch("api.websockets.connection.manager", fake_manager),
        patch("core.turns.orchestrator.mongodb") as mock_db,
        patch("core.turns.orchestrator.VoiceDelivery", return_value=voice),
        patch.object(orchestrator, "_execute_turn", new=AsyncMock()),
        patch("core.turns.orchestrator.event_bus.publish", new=AsyncMock()),
    ):
        mock_db.store_message = AsyncMock()
        await orchestrator.process_turn(
            connection_id="conn-mode-no-audio",
            audio_bytes=None,
            system_context="SYSTEM EVENT: Wake up",
            source="system",
            turn_id="turn-sys-1",
        )

    assert processor.mode == VoiceMode.ACTIVE_IDLE
    assert processor.last_source == "orchestrator.turn_finally_no_audio"


@pytest.mark.asyncio
async def test_cancelled_turn_with_audio_releases_active_ai_turn() -> None:
    class Processor:
        def __init__(self) -> None:
            self.mode = VoiceMode.ACTIVE_IDLE
            self.refresh_activity = MagicMock()

        def set_mode(self, mode: VoiceMode, *, source: str) -> None:
            self.mode = mode
            self.last_source = source

        def force_passive(self, *, reason: str = "unspecified", release_wake_refractory: bool = False) -> None:
            self.mode = VoiceMode.PASSIVE

    processor = Processor()
    session = SimpleNamespace(
        context={},
        owner_id="owner-1",
        connection_id="conn-mode-cancel",
        turn_lock=asyncio.Lock(),
        voice_turn=None,
        accepted_input_task=None,
        current_run_task=None,
        active_audio_turn_id="turn-cancel-1",
        soft_muted=False,
        processor=processor,
        last_turn_routed_tools=set(),
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock())
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    orchestrator.tts = MagicMock()

    voice = AsyncMock()
    voice.first_audio_sent = True
    voice.response_id = "resp-1"
    voice.start = AsyncMock()
    voice.aclose = AsyncMock()
    voice.send_tts_end_if_ready = AsyncMock()

    async def cancel_execute(*args, **kwargs):
        raise asyncio.CancelledError()

    with (
        patch("api.websockets.connection.manager", fake_manager),
        patch("core.turns.orchestrator.mongodb") as mock_db,
        patch("core.turns.orchestrator.VoiceDelivery", return_value=voice),
        patch.object(orchestrator, "_execute_turn", new=AsyncMock(side_effect=cancel_execute)),
        patch("core.turns.orchestrator.event_bus.publish", new=AsyncMock()),
    ):
        mock_db.store_message = AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.process_turn(
                connection_id="conn-mode-cancel",
                audio_bytes=None,
                system_context="SYSTEM EVENT: Wake up",
                source="system",
                turn_id="turn-cancel-1",
            )

    assert processor.mode == VoiceMode.ACTIVE_IDLE
    assert processor.last_source == "orchestrator.turn_finally_release"


@pytest.mark.asyncio
async def test_successful_audio_turn_defers_mode_to_playback_end() -> None:
    class Processor:
        def __init__(self) -> None:
            self.mode = VoiceMode.ACTIVE_IDLE
            self.refresh_activity = MagicMock()
            self.sources: list[str] = []

        def set_mode(self, mode: VoiceMode, *, source: str) -> None:
            self.mode = mode
            self.sources.append(source)

    processor = Processor()
    session = SimpleNamespace(
        context={},
        owner_id="owner-1",
        connection_id="conn-mode-defer",
        turn_lock=asyncio.Lock(),
        voice_turn=None,
        accepted_input_task=None,
        current_run_task=None,
        active_audio_turn_id="turn-defer-1",
        soft_muted=False,
        processor=processor,
        last_turn_routed_tools=set(),
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock())
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    orchestrator.tts = MagicMock()

    voice = AsyncMock()
    voice.first_audio_sent = True
    voice.response_id = "resp-1"
    voice.start = AsyncMock()
    voice.aclose = AsyncMock()
    voice.send_tts_end_if_ready = AsyncMock()

    with (
        patch("api.websockets.connection.manager", fake_manager),
        patch("core.turns.orchestrator.mongodb") as mock_db,
        patch("core.turns.orchestrator.VoiceDelivery", return_value=voice),
        patch.object(orchestrator, "_execute_turn", new=AsyncMock()),
        patch("core.turns.orchestrator.event_bus.publish", new=AsyncMock()),
    ):
        mock_db.store_message = AsyncMock()
        await orchestrator.process_turn(
            connection_id="conn-mode-defer",
            audio_bytes=None,
            system_context="SYSTEM EVENT: Wake up",
            source="system",
            turn_id="turn-defer-1",
        )

    assert processor.mode == VoiceMode.ACTIVE_AI_TURN
    assert "orchestrator.turn_start" in processor.sources
    assert "orchestrator.turn_finally_release" not in processor.sources
    assert "orchestrator.turn_finally_no_audio" not in processor.sources
    voice.send_tts_end_if_ready.assert_awaited()


@pytest.mark.asyncio
async def test_deliver_text_publishes_tts_end_after_audio() -> None:
    class Processor:
        def __init__(self) -> None:
            self.mode = VoiceMode.ACTIVE_IDLE
            self.sources: list[str] = []

        def set_mode(self, mode: VoiceMode, *, source: str) -> None:
            self.mode = mode
            self.sources.append(source)

    processor = Processor()
    session = SimpleNamespace(
        context={},
        owner_id="owner-1",
        connection_id="conn-deliver-text",
        presence=SimpleNamespace(node_id="node-1", location=None),
        turn_lock=asyncio.Lock(),
        current_run_task=None,
        processor=processor,
    )
    fake_manager = _manager_stub(session, send_voice_response=AsyncMock())
    orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
    orchestrator.tts = MagicMock()

    voice = AsyncMock()
    voice.first_audio_sent = True
    voice.start = AsyncMock()
    voice.aclose = AsyncMock()
    voice.on_stream = AsyncMock()
    voice.send_tts_end_if_ready = AsyncMock()

    with (
        patch("api.websockets.connection.manager", fake_manager),
        patch("core.turns.orchestrator.VoiceDelivery", return_value=voice),
    ):
        await orchestrator._deliver_text(
            "conn-deliver-text",
            "How did you sleep last night?",
            None,
            persist=False,
        )

    assert processor.mode == VoiceMode.ACTIVE_AI_TURN
    assert "orchestrator.deliver_text" in processor.sources
    assert "orchestrator.deliver_text_finally" not in processor.sources
    voice.send_tts_end_if_ready.assert_awaited()
    voice.aclose.assert_awaited()
