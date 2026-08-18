"""Turn-origin delivery: strict connection routing and absent-target behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.websockets.connection import ConnectionManager
from api.websockets.presence import build_presence_identity
from core.context import RuntimeIdentity, ToolRuntimeContext, bind_tool_context, reset_tool_context
from core.turns.orchestrator import AssistantOrchestrator
from services.events import Event, EventType


class _ImmediateTask:
    def add_done_callback(self, callback):
        callback(self)


def _make_session(connection_id: str, owner_id: str, node_id: str) -> SimpleNamespace:
    presence = build_presence_identity(
        {"owner_id": owner_id, "node_id": node_id},
        connection_id=connection_id,
        allow_owner_override=True,
    )
    processor = SimpleNamespace(
        mode=SimpleNamespace(name="ACTIVE_IDLE"),
        set_mode=MagicMock(),
        force_passive=MagicMock(),
    )
    return SimpleNamespace(
        websocket=AsyncMock(),
        processor=processor,
        presence=presence,
        owner_id=owner_id,
        connection_id=connection_id,
        last_active_at=None,
        connected_at=0.0,
        context={"timezone": "UTC", **presence.context()},
        turn_lock=__import__("asyncio").Lock(),
        voice_turn=None,
        current_run_task=None,
        accepted_input_task=None,
        current_delivery=None,
        first_audio_sent=False,
        last_turn_audio_sent=False,
        current_trigger_instance_id=None,
        soft_muted=False,
        tts_sentence_queue=None,
    )


def _orchestrator_with_manager(manager: ConnectionManager) -> AssistantOrchestrator:
    return AssistantOrchestrator(
        stt=MagicMock(),
        llm=MagicMock(),
        agent=MagicMock(),
        tts=MagicMock(),
    )


@pytest.mark.asyncio
async def test_two_nodes_coexist_and_strict_lookup_is_exact():
    manager = ConnectionManager()
    kitchen = _make_session("conn-kitchen", "home", "kitchen")
    browser = _make_session("conn-browser", "home", "browser")
    manager.sessions["conn-kitchen"] = kitchen
    manager.sessions["conn-browser"] = browser
    manager.default_connection_by_owner_id["home"] = "conn-browser"
    manager.active_connection_by_node_key["home:kitchen"] = "conn-kitchen"
    manager.active_connection_by_node_key["home:browser"] = "conn-browser"

    assert manager.get_session_by_connection("conn-kitchen") is kitchen
    assert manager.get_session_by_connection("conn-browser") is browser
    assert manager.get_session_by_connection("home") is None
    assert manager.get_default_session_for_owner("home") is browser


@pytest.mark.asyncio
async def test_deliver_text_targets_origin_connection_not_owner_default():
    manager = ConnectionManager()
    kitchen = _make_session("conn-kitchen", "home", "kitchen")
    browser = _make_session("conn-browser", "home", "browser")
    manager.sessions["conn-kitchen"] = kitchen
    manager.sessions["conn-browser"] = browser
    manager.default_connection_by_owner_id["home"] = "conn-browser"

    sent_targets: list[str] = []

    async def capture_send(target_id, message_type, data, *, message_id=None):
        sent_targets.append(target_id)
        return message_id or "msg"

    manager.send_voice_response = capture_send
    orch = _orchestrator_with_manager(manager)

    with patch("api.websockets.connection.manager", manager), \
        patch.object(orch, "_execute_turn", new=AsyncMock()), \
        patch("core.turns.delivery.VoiceDelivery") as voice_cls:
        voice = AsyncMock()
        voice.start = AsyncMock()
        voice.aclose = AsyncMock()
        voice.on_stream = AsyncMock()
        voice_cls.return_value = voice
        await orch._deliver_text("conn-kitchen", "Kitchen reply.", None, persist=False)

    assert sent_targets
    assert all(target == "conn-kitchen" for target in sent_targets)


@pytest.mark.asyncio
async def test_process_turn_missing_origin_returns_without_owner_fallback():
    manager = ConnectionManager()
    manager.default_connection_by_owner_id["home"] = "conn-browser"
    manager.sessions["conn-browser"] = _make_session("conn-browser", "home", "browser")

    orch = _orchestrator_with_manager(manager)
    execute = AsyncMock()
    orch._execute_turn = execute

    with patch("api.websockets.connection.manager", manager):
        await orch.process_turn(
            connection_id="conn-kitchen",
            audio_bytes=None,
            text="hello",
            source="user",
        )

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_turn_owner_id_is_not_accepted_as_connection():
    manager = ConnectionManager()
    manager.sessions["conn-browser"] = _make_session("conn-browser", "home", "browser")
    manager.default_connection_by_owner_id["home"] = "conn-browser"

    orch = _orchestrator_with_manager(manager)
    execute = AsyncMock()
    orch._execute_turn = execute

    with patch("api.websockets.connection.manager", manager):
        await orch.process_turn(
            connection_id="home",
            audio_bytes=None,
            text="hello",
            source="user",
        )

    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_trigger_without_audio_gets_scheduled_retry():
    orch = _orchestrator_with_manager(ConnectionManager())
    session = SimpleNamespace(
        last_turn_audio_sent=False,
        last_turn_audio_completed=False,
        current_trigger_instance_id=None,
    )
    trigger_service = SimpleNamespace(
        mark_awaiting_delivery=AsyncMock(return_value=True),
        mark_delivered=AsyncMock(return_value=True),
    )
    runner = orch._wrap_with_trigger_delivery_finalize(
        AsyncMock(),
        "trg-test",
        session,
    )

    with patch("core.triggers.service.trigger_service", trigger_service):
        await runner()

    trigger_service.mark_delivered.assert_not_awaited()
    call = trigger_service.mark_awaiting_delivery.await_args
    assert call.args == ("trg-test",)
    assert call.kwargs["reason"] == "no_audio_sent"
    assert call.kwargs["next_retry_at"] is not None


@pytest.mark.asyncio
async def test_trigger_with_partial_audio_gets_scheduled_retry():
    orch = _orchestrator_with_manager(ConnectionManager())
    session = SimpleNamespace(
        last_turn_audio_sent=False,
        last_turn_audio_completed=False,
        current_trigger_instance_id=None,
    )
    trigger_service = SimpleNamespace(
        mark_awaiting_delivery=AsyncMock(return_value=True),
        mark_delivered=AsyncMock(return_value=True),
    )

    async def partial_delivery() -> None:
        session.last_turn_audio_sent = True
        session.last_turn_audio_completed = False

    runner = orch._wrap_with_trigger_delivery_finalize(
        partial_delivery,
        "trg-test",
        session,
    )

    with patch("core.triggers.service.trigger_service", trigger_service):
        await runner()

    trigger_service.mark_delivered.assert_not_awaited()
    trigger_service.mark_awaiting_delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_delivered_trigger_carries_its_routed_tools_to_next_user_turn():
    orch = _orchestrator_with_manager(ConnectionManager())
    session = SimpleNamespace(
        connection_id="conn-bedroom",
        last_turn_audio_sent=False,
        last_turn_audio_completed=False,
        last_turn_routed_tools=set(),
        current_trigger_instance_id=None,
    )
    trigger_service = SimpleNamespace(
        mark_awaiting_delivery=AsyncMock(return_value=True),
        mark_delivered=AsyncMock(return_value=True),
    )
    router = SimpleNamespace(record_route_carryover=MagicMock())

    async def delivered() -> None:
        session.last_turn_audio_sent = True
        session.last_turn_audio_completed = True
        session.last_turn_routed_tools = {"habits.log_habit_by_name"}

    runner = orch._wrap_with_trigger_delivery_finalize(
        delivered,
        "trg-test",
        session,
    )

    with (
        patch("core.triggers.service.trigger_service", trigger_service),
        patch("core.tool_router.tool_router", router),
    ):
        await runner()

    router.record_route_carryover.assert_called_once_with(
        "conn-bedroom",
        tools={"habits.log_habit_by_name"},
    )
    assert session.last_turn_routed_tools == set()


@pytest.mark.asyncio
async def test_protocol_run_publishes_connection_id():
    from plugins import protocol as protocol_plugin

    published: list[Event] = []

    async def capture_publish(event: Event) -> None:
        published.append(event)

    class ProtocolDb:
        def __getitem__(self, name):
            assert name == protocol_plugin.COLLECTION
            return SimpleNamespace(
                find_one=AsyncMock(return_value={"id": "protocol-1", "name": "Morning"}),
                update_one=AsyncMock(),
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(protocol_plugin.event_bus, "publish", capture_publish)
    monkeypatch.setattr(protocol_plugin.mongodb, "db", ProtocolDb())

    token = bind_tool_context(
        ToolRuntimeContext(
            identity=RuntimeIdentity(
                owner_id="home",
                connection_id="conn-kitchen",
                node_id="kitchen",
            ),
            timezone="UTC",
        )
    )
    try:
        await protocol_plugin.ProtocolPlugin().run_protocol("Morning")
    finally:
        reset_tool_context(token)
        monkeypatch.undo()

    assert published
    data = published[0].data
    assert data["connection_id"] == "conn-kitchen"
    assert data["node_id"] == "kitchen"


@pytest.mark.asyncio
async def test_protocol_run_omits_connection_id_for_background_owner_context():
    from plugins import protocol as protocol_plugin

    published: list[Event] = []

    async def capture_publish(event: Event) -> None:
        published.append(event)

    class ProtocolDb:
        def __getitem__(self, name):
            assert name == protocol_plugin.COLLECTION
            return SimpleNamespace(
                find_one=AsyncMock(return_value={"id": "protocol-1", "name": "Morning"}),
                update_one=AsyncMock(),
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(protocol_plugin.event_bus, "publish", capture_publish)
    monkeypatch.setattr(protocol_plugin.mongodb, "db", ProtocolDb())

    token = bind_tool_context(
        ToolRuntimeContext(
            identity=RuntimeIdentity(owner_id="home", connection_id="home"),
            timezone="UTC",
        )
    )
    try:
        await protocol_plugin.ProtocolPlugin().run_protocol("Morning")
    finally:
        reset_tool_context(token)
        monkeypatch.undo()

    assert published
    assert published[0].data == {"owner_id": "home", "protocol_name": "Morning"}


@pytest.mark.asyncio
async def test_protocol_run_handler_uses_event_connection_id():
    manager = ConnectionManager()
    kitchen = _make_session("conn-kitchen", "home", "kitchen")
    browser = _make_session("conn-browser", "home", "browser")
    manager.sessions["conn-kitchen"] = kitchen
    manager.sessions["conn-browser"] = browser
    manager.default_connection_by_owner_id["home"] = "conn-browser"

    orch = _orchestrator_with_manager(manager)
    scheduled: list[str] = []

    def capture_create_task(coro):
        scheduled.append("task")
        coro.close()
        return MagicMock()

    with patch("api.websockets.connection.manager", manager), \
        patch("core.turns.orchestrator.build_protocol_context", new=AsyncMock(return_value="ctx")), \
        patch("core.turns.orchestrator.build_system_turn_message", return_value="system"), \
        patch("core.turns.orchestrator.asyncio.create_task", side_effect=capture_create_task):
        await orch._handle_protocol_run(
            Event(
                type=EventType.PROTOCOL_RUN,
                source="test",
                data={
                    "owner_id": "home",
                    "protocol_name": "Morning",
                    "connection_id": "conn-kitchen",
                },
            )
        )

    assert scheduled


@pytest.mark.asyncio
async def test_protocol_run_handler_owner_default_when_no_origin_connection():
    manager = ConnectionManager()
    browser = _make_session("conn-browser", "home", "browser")
    manager.sessions["conn-browser"] = browser
    manager.default_connection_by_owner_id["home"] = "conn-browser"

    orch = _orchestrator_with_manager(manager)
    scheduled: list[str] = []

    def capture_create_task(coro):
        scheduled.append("task")
        coro.close()
        return _ImmediateTask()

    def fake_run_and_log(protocol_name, owner_id, triggered_by, runner, turn_id):
        async def noop():
            return None

        return noop()

    with patch("api.websockets.connection.manager", manager), \
        patch("core.turns.orchestrator.build_protocol_context", new=AsyncMock(return_value="ctx")), \
        patch("core.turns.orchestrator.build_system_turn_message", return_value="system"), \
        patch.object(orch, "_run_and_log_protocol", new=fake_run_and_log), \
        patch("core.turns.orchestrator.asyncio.create_task", side_effect=capture_create_task):
        await orch._handle_protocol_run(
            Event(
                type=EventType.PROTOCOL_RUN,
                source="test",
                data={"owner_id": "home", "protocol_name": "Morning"},
            )
        )

    assert scheduled == ["task"]


@pytest.mark.asyncio
async def test_stop_listening_publishes_origin_connection_id():
    from plugins import system as system_plugin

    published: list[Event] = []

    async def capture_publish(event: Event) -> None:
        published.append(event)

    with patch.object(system_plugin.event_bus, "publish", capture_publish):
        token = bind_tool_context(
            ToolRuntimeContext(
                identity=RuntimeIdentity(
                    owner_id="home",
                    connection_id="conn-kitchen",
                    node_id="kitchen",
                ),
                timezone="UTC",
            )
        )
        try:
            await system_plugin.SystemPlugin().stop_listening()
        finally:
            reset_tool_context(token)

    data = published[0].data
    assert data["session_id"] == "conn-kitchen"
    assert data["connection_id"] == "conn-kitchen"


@pytest.mark.asyncio
async def test_session_end_with_stale_connection_is_noop():
    manager = ConnectionManager()
    orch = _orchestrator_with_manager(manager)

    with patch("api.websockets.connection.manager", manager):
        await orch._handle_session_end(
            Event(
                type=EventType.VOICE_SESSION_END,
                source="test",
                data={"session_id": "conn-gone"},
            ),
        )


@pytest.mark.asyncio
async def test_trigger_due_routes_to_target_node_not_owner_default():
    import time
    from core.triggers.models import (
        AttentionPolicy,
        DeliveryPlan,
        DeliveryTargetHint,
        FreshnessPolicy,
        TriggerAction,
        TriggerInstance,
        TriggerOrigin,
    )

    manager = ConnectionManager()
    kitchen = _make_session("conn-bedroom", "home", "bedroom")
    browser = _make_session("conn-browser", "home", "browser")
    kitchen.last_active_at = time.time() - 100
    browser.last_active_at = time.time()
    manager.sessions["conn-bedroom"] = kitchen
    manager.sessions["conn-browser"] = browser
    manager.default_connection_by_owner_id["home"] = "conn-browser"
    manager.active_connection_by_node_key["home:bedroom"] = "conn-bedroom"
    manager.active_connection_by_node_key["home:browser"] = "conn-browser"

    sent_targets: list[str] = []
    manager.send_voice_response = AsyncMock(side_effect=lambda target_id, *_a, **_k: sent_targets.append(target_id) or "msg")

    orch = _orchestrator_with_manager(manager)
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    instance = TriggerInstance(
        id="inst-bedroom",
        rule_id=None,
        owner_id="home",
        status="claimed",
        due_at=now,
        created_at=now,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=now),
        action_snapshot=TriggerAction(decision="tell", message="Wake up"),
        attention_snapshot=AttentionPolicy(sound="alarm", requires_ack=True),
        delivery_snapshot=DeliveryPlan(target=DeliveryTargetHint(node_id="bedroom")),
        freshness_snapshot=FreshnessPolicy(),
        management={"provider": "scheduler", "resource_id": "rule-wake"},
    )

    with patch("api.websockets.connection.manager", manager), \
        patch("core.triggers.service.trigger_service.get_instance", new=AsyncMock(return_value=instance)), \
        patch(
            "core.triggers.service.trigger_service.get_rule",
            new=AsyncMock(return_value=SimpleNamespace(enabled=True, paused_until=None)),
        ), \
        patch("core.triggers.service.trigger_service.mark_executing", new=AsyncMock(return_value=True)), \
        patch("core.attention.service.attention_service.get_mode", new=AsyncMock(return_value="active")), \
        patch("core.triggers.service.trigger_service.record_turn_id", new=AsyncMock()), \
        patch.object(orch, "_schedule_runner", side_effect=lambda runner, *_a, **_k: _ImmediateTask()):
        await orch._handle_trigger_due(
            Event(
                type=EventType.TRIGGER_DUE,
                source="test",
                data={"instance_id": "inst-bedroom", "owner_id": "home"},
            )
        )

    assert sent_targets
    assert sent_targets[0] == "conn-bedroom"


@pytest.mark.asyncio
async def test_trigger_due_without_live_session_marks_awaiting_delivery():
    from core.triggers.models import AttentionPolicy, DeliveryPlan, FreshnessPolicy, TriggerAction, TriggerInstance, TriggerOrigin

    manager = ConnectionManager()
    orch = _orchestrator_with_manager(manager)
    mark_awaiting = AsyncMock()

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    instance = TriggerInstance(
        id="inst-1",
        rule_id="rule-1",
        owner_id="home",
        status="claimed",
        due_at=now,
        created_at=now,
        origin_snapshot=TriggerOrigin(kind="system"),
        action_snapshot=TriggerAction(decision="tell", message="Ping"),
        attention_snapshot=AttentionPolicy(),
        delivery_snapshot=DeliveryPlan(),
        freshness_snapshot=FreshnessPolicy(),
        management={"provider": "system", "resource_id": "inst-1"},
    )

    with patch("api.websockets.connection.manager", manager), \
        patch("core.triggers.service.trigger_service.get_instance", new=AsyncMock(return_value=instance)), \
        patch(
            "core.triggers.service.trigger_service.get_rule",
            new=AsyncMock(return_value=SimpleNamespace(enabled=True, paused_until=None)),
        ), \
        patch("core.triggers.service.trigger_service.mark_executing", new=AsyncMock(return_value=True)), \
        patch("core.attention.service.attention_service.get_mode", new=AsyncMock(return_value="active")), \
        patch("core.triggers.service.trigger_service.record_turn_id", new=AsyncMock()), \
        patch("core.triggers.service.trigger_service.mark_awaiting_delivery", mark_awaiting):
        await orch._handle_trigger_due(
            Event(
                type=EventType.TRIGGER_DUE,
                source="test",
                data={"instance_id": "inst-1", "owner_id": "home"},
            )
        )

    mark_awaiting.assert_awaited_once_with("inst-1", reason="no_speaker_endpoint")


@pytest.mark.asyncio
async def test_trigger_due_location_target_offline_marks_awaiting_delivery():
    from core.triggers.models import (
        AttentionPolicy,
        DeliveryPlan,
        DeliveryTargetHint,
        FreshnessPolicy,
        TriggerAction,
        TriggerInstance,
        TriggerOrigin,
    )

    manager = ConnectionManager()
    browser = _make_session("conn-browser", "home", "browser")
    manager.sessions["conn-browser"] = browser
    manager.default_connection_by_owner_id["home"] = "conn-browser"

    orch = _orchestrator_with_manager(manager)
    mark_awaiting = AsyncMock()

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    instance = TriggerInstance(
        id="inst-bedroom",
        rule_id=None,
        owner_id="home",
        status="claimed",
        due_at=now,
        created_at=now,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=now),
        action_snapshot=TriggerAction(decision="tell", message="Wake up"),
        attention_snapshot=AttentionPolicy(sound="alarm", requires_ack=True),
        delivery_snapshot=DeliveryPlan(
            target=DeliveryTargetHint(
                location_ref={"room_id": "bedroom", "ha_area_id": "area-bedroom"},
            ),
        ),
        freshness_snapshot=FreshnessPolicy(),
        management={"provider": "scheduler", "resource_id": "inst-location"},
    )

    with patch("api.websockets.connection.manager", manager), \
        patch("core.triggers.service.trigger_service.get_instance", new=AsyncMock(return_value=instance)), \
        patch("core.triggers.service.trigger_service.mark_executing", new=AsyncMock(return_value=True)), \
        patch("core.attention.service.attention_service.get_mode", new=AsyncMock(return_value="active")), \
        patch("core.triggers.service.trigger_service.record_turn_id", new=AsyncMock()), \
        patch("core.triggers.service.trigger_service.mark_awaiting_delivery", mark_awaiting):
        await orch._handle_trigger_due(
            Event(
                type=EventType.TRIGGER_DUE,
                source="test",
                data={"instance_id": "inst-bedroom", "owner_id": "home"},
            )
        )

    mark_awaiting.assert_awaited_once_with("inst-bedroom", reason="target_location_offline")


@pytest.mark.asyncio
async def test_user_turn_disconnect_mid_turn_settles_without_owner_default_delivery():
    manager = ConnectionManager()
    kitchen = _make_session("conn-kitchen", "home", "kitchen")
    browser = _make_session("conn-browser", "home", "browser")
    manager.sessions["conn-kitchen"] = kitchen
    manager.sessions["conn-browser"] = browser
    manager.default_connection_by_owner_id["home"] = "conn-browser"

    sent_targets: list[str] = []

    async def capture_send(target_id, message_type, data, *, message_id=None):
        sent_targets.append(target_id)
        return message_id or "msg"

    async def fake_execute_turn(*args, result, **kwargs):
        manager.sessions.pop("conn-kitchen", None)
        result.turn_trace.append(("user", "hello"))
        result.turn_trace.append(("assistant", "hi", {"turn_type": "text_only"}))
        result.full_response = "hi"

    manager.send_voice_response = capture_send
    orch = _orchestrator_with_manager(manager)

    with patch("api.websockets.connection.manager", manager), \
        patch("core.turns.orchestrator.mongodb") as mock_db, \
        patch.object(orch, "_execute_turn", new=AsyncMock(side_effect=fake_execute_turn)):
        mock_db.upsert_user_turn = AsyncMock()
        mock_db.mark_user_turn_status = AsyncMock()
        mock_db.store_message = AsyncMock()

        await orch.process_turn(
            connection_id="conn-kitchen",
            audio_bytes=b"pcm",
            text="hello",
            turn_id="turn-mid-drop",
        )

    assert "conn-browser" not in sent_targets
    mock_db.mark_user_turn_status.assert_awaited_once_with(
        "home", "turn-mid-drop", "completed", delivery=None
    )
    assert kitchen.processor.set_mode.called


@pytest.mark.asyncio
async def test_retry_awaiting_ring_budget_quiet_settles_requires_ack():
    from datetime import datetime, timezone

    from core.triggers.models import (
        AttentionPolicy,
        DeliveryPlan,
        FreshnessPolicy,
        TriggerAction,
        TriggerInstance,
        TriggerOrigin,
    )
    from core.turns.orchestrator import ACK_MAX_DELIVERY_ATTEMPTS

    manager = ConnectionManager()
    session = _make_session("conn-1", "home", "bedroom")
    manager.sessions["conn-1"] = session
    manager.default_connection_by_owner_id["home"] = "conn-1"
    orch = _orchestrator_with_manager(manager)

    now = datetime.now(timezone.utc)
    instance = TriggerInstance(
        id="inst-alarm",
        rule_id=None,
        owner_id="home",
        status="awaiting_delivery",
        due_at=now,
        created_at=now,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=now),
        action_snapshot=TriggerAction(decision="tell", message="Wake up"),
        attention_snapshot=AttentionPolicy(sound="alarm", requires_ack=True),
        delivery_snapshot=DeliveryPlan(),
        freshness_snapshot=FreshnessPolicy(),
        management={"provider": "scheduler", "resource_id": "rule-wake"},
        turn_ids=[f"turn-{i}" for i in range(ACK_MAX_DELIVERY_ATTEMPTS)],
    )
    claim = AsyncMock(return_value=True)
    mark_delivered = AsyncMock(return_value=True)
    publish = AsyncMock()

    with (
        patch("api.websockets.connection.manager", manager),
        patch(
            "core.triggers.service.trigger_service.get_awaiting_delivery",
            new=AsyncMock(return_value=[instance]),
        ),
        patch(
            "core.triggers.service.trigger_service.dedupe_awaiting_for_retry",
            new=AsyncMock(side_effect=lambda instances: instances),
        ),
        patch("core.triggers.service.trigger_service.claim_awaiting_instance", new=claim),
        patch("core.triggers.service.trigger_service.mark_delivered", new=mark_delivered),
        patch("core.turns.orchestrator.event_bus.publish", new=publish),
    ):
        await orch._handle_trigger_retry_awaiting(
            Event(
                type=EventType.TRIGGER_RETRY_AWAITING,
                source="test",
                data={"owner_id": "home"},
            )
        )

    claim.assert_awaited_once_with("inst-alarm")
    mark_delivered.assert_awaited_once_with(
        "inst-alarm",
        result_text="ring_budget_exhausted",
    )
    assert all(
        not (isinstance(call.args[0], Event) and call.args[0].type == EventType.TRIGGER_DUE)
        for call in publish.await_args_list
    )


@pytest.mark.asyncio
async def test_retry_awaiting_below_budget_republishes_trigger_due():
    from datetime import datetime, timezone

    from core.triggers.models import (
        AttentionPolicy,
        DeliveryPlan,
        FreshnessPolicy,
        TriggerAction,
        TriggerInstance,
        TriggerOrigin,
    )

    manager = ConnectionManager()
    session = _make_session("conn-1", "home", "bedroom")
    manager.sessions["conn-1"] = session
    manager.default_connection_by_owner_id["home"] = "conn-1"
    orch = _orchestrator_with_manager(manager)

    now = datetime.now(timezone.utc)
    instance = TriggerInstance(
        id="inst-alarm",
        rule_id=None,
        owner_id="home",
        status="awaiting_delivery",
        due_at=now,
        created_at=now,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=now),
        action_snapshot=TriggerAction(decision="tell", message="Wake up"),
        attention_snapshot=AttentionPolicy(sound="alarm", requires_ack=True),
        delivery_snapshot=DeliveryPlan(),
        freshness_snapshot=FreshnessPolicy(),
        management={"provider": "scheduler", "resource_id": "rule-wake"},
        turn_ids=["turn-1", "turn-2"],
    )
    claim = AsyncMock(return_value=True)
    mark_delivered = AsyncMock()
    published: list[Event] = []

    async def capture_publish(event: Event):
        published.append(event)

    with (
        patch("api.websockets.connection.manager", manager),
        patch(
            "core.triggers.service.trigger_service.get_awaiting_delivery",
            new=AsyncMock(return_value=[instance]),
        ),
        patch(
            "core.triggers.service.trigger_service.dedupe_awaiting_for_retry",
            new=AsyncMock(side_effect=lambda instances: instances),
        ),
        patch("core.triggers.service.trigger_service.claim_awaiting_instance", new=claim),
        patch("core.triggers.service.trigger_service.mark_delivered", new=mark_delivered),
        patch("core.turns.orchestrator.event_bus.publish", new=AsyncMock(side_effect=capture_publish)),
    ):
        await orch._handle_trigger_retry_awaiting(
            Event(
                type=EventType.TRIGGER_RETRY_AWAITING,
                source="test",
                data={"owner_id": "home"},
            )
        )

    mark_delivered.assert_not_called()
    claim.assert_awaited_once_with("inst-alarm")
    assert any(event.type == EventType.TRIGGER_DUE for event in published)


@pytest.mark.asyncio
async def test_retry_awaiting_non_ack_ignores_ring_budget():
    from datetime import datetime, timezone

    from core.triggers.models import (
        AttentionPolicy,
        DeliveryPlan,
        FreshnessPolicy,
        TriggerAction,
        TriggerInstance,
        TriggerOrigin,
    )
    from core.turns.orchestrator import ACK_MAX_DELIVERY_ATTEMPTS

    manager = ConnectionManager()
    session = _make_session("conn-1", "home", "bedroom")
    manager.sessions["conn-1"] = session
    manager.default_connection_by_owner_id["home"] = "conn-1"
    orch = _orchestrator_with_manager(manager)

    now = datetime.now(timezone.utc)
    instance = TriggerInstance(
        id="inst-reminder",
        rule_id=None,
        owner_id="home",
        status="awaiting_delivery",
        due_at=now,
        created_at=now,
        origin_snapshot=TriggerOrigin(kind="time", fire_at=now),
        action_snapshot=TriggerAction(decision="tell", message="Reminder"),
        attention_snapshot=AttentionPolicy(sound="chime", requires_ack=False),
        delivery_snapshot=DeliveryPlan(),
        freshness_snapshot=FreshnessPolicy(),
        management={"provider": "scheduler", "resource_id": "rule-reminder"},
        turn_ids=[f"turn-{i}" for i in range(ACK_MAX_DELIVERY_ATTEMPTS + 3)],
    )
    claim = AsyncMock(return_value=True)
    mark_delivered = AsyncMock()
    published: list[Event] = []

    async def capture_publish(event: Event):
        published.append(event)

    with (
        patch("api.websockets.connection.manager", manager),
        patch(
            "core.triggers.service.trigger_service.get_awaiting_delivery",
            new=AsyncMock(return_value=[instance]),
        ),
        patch(
            "core.triggers.service.trigger_service.dedupe_awaiting_for_retry",
            new=AsyncMock(side_effect=lambda instances: instances),
        ),
        patch("core.triggers.service.trigger_service.claim_awaiting_instance", new=claim),
        patch("core.triggers.service.trigger_service.mark_delivered", new=mark_delivered),
        patch("core.turns.orchestrator.event_bus.publish", new=AsyncMock(side_effect=capture_publish)),
    ):
        await orch._handle_trigger_retry_awaiting(
            Event(
                type=EventType.TRIGGER_RETRY_AWAITING,
                source="test",
                data={"owner_id": "home"},
            )
        )

    mark_delivered.assert_not_called()
    assert any(event.type == EventType.TRIGGER_DUE for event in published)
