"""Tests for the attention system: models, service decisions, and plugin."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.attention.models import AttentionState
from core.attention.service import AttentionService
from core.triggers.delivery_policy import (
    resolve_proactive_speech_delivery,
    resolve_trigger_delivery,
)
from core.triggers.models import AttentionPolicy, DeliveryPlan, FreshnessPolicy, TriggerAction, TriggerInstance, TriggerOrigin
from core.turns.delivery import TurnResult


def _policy(level="normal", sound="chime", requires_ack=False) -> AttentionPolicy:
    return AttentionPolicy(level=level, sound=sound, requires_ack=requires_ack)


def _delivery() -> DeliveryPlan:
    return DeliveryPlan()


class TestTriggerDeliveryResolution:
    def test_act_runs_headless_and_never_speaks(self):
        resolution = resolve_trigger_delivery(
            attention_mode="active",
            attention=_policy("normal"),
            delivery=_delivery(),
            decision="act",
        )
        assert resolution.agent_execution == "headless"
        assert resolution.presentation == "never"
        assert resolution.delivery_tag == "announce"
        assert resolution.reason == "decision_act"
        assert resolution.blocked_result is None

    def test_offer_runs_headless_and_speaks_only_with_content(self):
        resolution = resolve_trigger_delivery(
            attention_mode="paused",
            attention=_policy("critical"),
            delivery=_delivery(),
            decision="offer",
        )
        assert resolution.agent_execution == "headless"
        assert resolution.presentation == "if_content"
        assert resolution.delivery_tag == "evaluate"
        assert resolution.reason == "decision_offer"
        assert resolution.blocked_result is None

    def test_tell_quiet_normal_defers_before_execution(self):
        resolution = resolve_trigger_delivery(
            attention_mode="quiet",
            attention=_policy("normal"),
            delivery=_delivery(),
            decision="tell",
        )
        assert resolution.agent_execution == "user_facing"
        assert resolution.presentation == "always"
        assert resolution.blocked_result == "awaiting_delivery"
        assert resolution.reason == "quiet_deferred"

    def test_final_speech_resolution_defers_while_paused(self):
        resolution = resolve_proactive_speech_delivery(
            attention_mode="paused",
            attention=_policy("critical"),
            delivery_tag="evaluate",
        )
        assert resolution.blocked_result == "awaiting_delivery"
        assert resolution.reason == "paused_deferred"

    def test_urgent_breaks_through_quiet(self):
        resolution = resolve_trigger_delivery(
            attention_mode="quiet",
            attention=_policy("urgent"),
            delivery=_delivery(),
            decision="tell",
        )
        assert resolution.blocked_result is None
        assert resolution.reason == "quiet_passthrough"

    def test_critical_does_not_pierce_paused_today(self):
        # No safety-class source exists yet, so critical is deferred under paused.
        resolution = resolve_trigger_delivery(
            attention_mode="paused",
            attention=_policy("critical"),
            delivery=_delivery(),
            decision="tell",
        )
        assert resolution.blocked_result == "awaiting_delivery"


# ---------------------------------------------------------------------------
# AttentionService tests (Mongo mocked)
# ---------------------------------------------------------------------------


class TestAttentionService:
    def _make_service(self):
        svc = AttentionService()
        return svc

    def _make_mock_mongodb(self, find_one_result=None):
        """Build a mock that behaves like the mongodb singleton used inside service methods."""
        mock_col = AsyncMock()
        mock_col.find_one = AsyncMock(return_value=find_one_result)
        mock_col.update_one = AsyncMock()
        schedule_col = AsyncMock()

        class _Cursor:
            def sort(self, *_args, **_kwargs):
                return self

            async def to_list(self, _limit):
                return []

        schedule_col.find = MagicMock(return_value=_Cursor())
        mock_mongodb = AsyncMock()
        mock_mongodb.db = MagicMock()

        def getitem(name):
            if name == "attention_schedules":
                return schedule_col
            return mock_col

        mock_mongodb.db.__getitem__ = MagicMock(side_effect=getitem)
        return mock_mongodb, mock_col

    @pytest.mark.asyncio
    async def test_get_state_defaults_to_active_when_no_doc(self):
        svc = self._make_service()
        mock_mongodb, _ = self._make_mock_mongodb(find_one_result=None)
        with patch("core.attention.service.mongodb", mock_mongodb):
            state = await svc.get_state("owner-1")
        assert state.mode == "active"
        assert state.owner_id == "owner-1"

    @pytest.mark.asyncio
    async def test_get_state_returns_override_mode(self):
        svc = self._make_service()
        doc = {
            "owner_id": "owner-1",
            "override": {
                "mode": "quiet",
                "source": "tool",
                "set_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": None,
            },
        }
        mock_mongodb, _ = self._make_mock_mongodb(find_one_result=doc)
        with patch("core.attention.service.mongodb", mock_mongodb):
            state = await svc.get_state("owner-1")
        assert state.mode == "quiet"

    @pytest.mark.asyncio
    async def test_get_state_auto_expires_timed_override(self):
        svc = self._make_service()
        now = datetime.now(timezone.utc)
        doc = {
            "owner_id": "owner-1",
            "override": {
                "mode": "quiet",
                "source": "tool",
                "set_at": (now - timedelta(minutes=10)).isoformat(),
                "expires_at": (now - timedelta(minutes=1)).isoformat(),
            },
        }
        mock_mongodb, _ = self._make_mock_mongodb(find_one_result=doc)
        with patch("core.attention.service.mongodb", mock_mongodb):
            state = await svc.get_state("owner-1")
        # Expired override falls through to the default (active).
        assert state.mode == "active"

    @pytest.mark.asyncio
    async def test_set_mode_upserts_to_mongo(self):
        svc = self._make_service()
        mock_mongodb, mock_col = self._make_mock_mongodb()
        with patch("core.attention.service.mongodb", mock_mongodb):
            state = await svc.set_mode("owner-1", "paused", source="tool")
        assert state.mode == "paused"
        mock_col.update_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_mode_with_duration_sets_expires_at(self):
        svc = self._make_service()
        mock_mongodb, _ = self._make_mock_mongodb()
        with patch("core.attention.service.mongodb", mock_mongodb):
            state = await svc.set_mode("owner-1", "quiet", duration_minutes=30)
        assert state.expires_at is not None
        delta = state.expires_at - datetime.now(timezone.utc)
        assert 28 * 60 < delta.total_seconds() < 32 * 60


# ---------------------------------------------------------------------------
# DeliveryPlan field tests
# ---------------------------------------------------------------------------


class TestDeliveryPlanDefaults:
    def test_default_channel_is_voice(self):
        plan = DeliveryPlan()
        assert plan.channel == "voice"

    def test_accepts_target_hint(self):
        from core.triggers.models import DeliveryTargetHint

        plan = DeliveryPlan(target=DeliveryTargetHint(node_id="node-1"))
        assert plan.target.node_id == "node-1"


# ---------------------------------------------------------------------------
# Preset decision tests
# ---------------------------------------------------------------------------


class TestPresetDecisions:
    def test_reminder_notify_uses_tell(self):
        from core.triggers.presets import reminder_preset
        from datetime import timezone
        preset = reminder_preset(
            owner_id="o1",
            message="standup",
            fire_at=datetime.now(timezone.utc),
        )
        assert preset["action"].decision == "tell"

    def test_reminder_with_instructions_uses_offer(self):
        from core.triggers.presets import reminder_preset
        from datetime import timezone
        preset = reminder_preset(
            owner_id="o1",
            message="check calendar",
            fire_at=datetime.now(timezone.utc),
            instructions="evaluate my calendar",
            decision="offer",
        )
        assert preset["action"].decision == "offer"
        assert preset["action"].instructions == "evaluate my calendar"

    def test_timer_uses_tell(self):
        from core.triggers.presets import timer_preset
        preset = timer_preset(owner_id="o1", message="timer done", duration_s=60)
        assert preset["action"].decision == "tell"

    def test_alarm_uses_tell(self):
        from core.triggers.presets import alarm_preset
        from datetime import timezone
        preset = alarm_preset(owner_id="o1", message="alarm", fire_at=datetime.now(timezone.utc))
        assert preset["action"].decision == "tell"

    def test_system_defaults_to_offer(self):
        from core.triggers.presets import system_preset
        preset = system_preset(owner_id="o1", message="health check")
        assert preset["action"].decision == "offer"

    def test_automation_default_is_tell(self):
        from core.triggers.presets import automation_preset
        preset = automation_preset(
            owner_id="o1", message="event", source="calendar", event="starting"
        )
        assert preset["action"].decision == "tell"

    def test_automation_act_decision(self):
        from core.triggers.presets import automation_preset
        preset = automation_preset(
            owner_id="o1", message="event", source="calendar", event="starting",
            decision="act",
            instructions="Mute notifications.",
        )
        assert preset["action"].decision == "act"


class TestAttentionPlugin:
    @pytest.mark.asyncio
    async def test_set_mode_uses_bound_owner_context(self, tool_context):
        from plugins.attention import AttentionPlugin

        with patch("plugins.attention.attention_service") as mock_service:
            mock_service.set_mode = AsyncMock(
                return_value=AttentionState(owner_id="owner-1", mode="quiet")
            )
            with patch("plugins.attention._resume_live_session", AsyncMock()) as resume:
                result = await AttentionPlugin().set_mode("quiet", duration_minutes=15)

        mock_service.set_mode.assert_awaited_once_with(
            "owner-1", "quiet", duration_minutes=15, source="tool"
        )
        resume.assert_not_awaited()
        assert "Going quiet" in result

    @pytest.mark.asyncio
    async def test_set_mode_accepts_duration_string(self, tool_context):
        from plugins.attention import AttentionPlugin

        with patch("plugins.attention.attention_service") as mock_service:
            mock_service.set_mode = AsyncMock(
                return_value=AttentionState(owner_id="owner-1", mode="quiet")
            )
            await AttentionPlugin().set_mode("quiet", duration_minutes="1h 30m")

        mock_service.set_mode.assert_awaited_once_with(
            "owner-1", "quiet", duration_minutes=90, source="tool"
        )

    @pytest.mark.asyncio
    async def test_tool_mute_soft_mutes_live_session(self, tool_context):
        from plugins.attention import AttentionPlugin

        session = SimpleNamespace(
            soft_muted=False,
            processor=SimpleNamespace(force_passive=MagicMock(), force_active=MagicMock()),
        )
        fake_manager = SimpleNamespace(get_session=MagicMock(return_value=session))
        with (
            patch("api.websockets.connection.manager", fake_manager),
            patch("plugins.attention.attention_service") as mock_service,
        ):
            mock_service.set_mode = AsyncMock(
                return_value=AttentionState(owner_id="owner-1", mode="quiet")
            )
            result = await AttentionPlugin().mute()

        mock_service.set_mode.assert_awaited_once_with(
            "owner-1", "quiet", duration_minutes=None, source="tool"
        )
        fake_manager.get_session.assert_called_once_with("conn-1")
        assert session.soft_muted is True
        session.processor.force_passive.assert_called_once_with(reason="attention_tool.mute")
        session.processor.force_active.assert_not_called()
        assert "Muted" in result

    @pytest.mark.asyncio
    async def test_tool_mute_with_duration_schedules_auto_resume(self, tool_context):
        from plugins.attention import AttentionPlugin

        session = SimpleNamespace(
            soft_muted=False,
            soft_mute_resume_task=None,
            processor=SimpleNamespace(force_passive=MagicMock(), force_active=MagicMock()),
        )
        fake_manager = SimpleNamespace(get_session=MagicMock(return_value=session))
        with (
            patch("api.websockets.connection.manager", fake_manager),
            patch("plugins.attention.attention_service") as mock_service,
        ):
            mock_service.set_mode = AsyncMock(
                return_value=AttentionState(owner_id="owner-1", mode="quiet")
            )
            await AttentionPlugin().mute(duration_minutes=15)

        mock_service.set_mode.assert_awaited_once_with(
            "owner-1", "quiet", duration_minutes=15, source="tool"
        )
        assert session.soft_muted is True
        assert session.soft_mute_resume_task is not None
        session.soft_mute_resume_task.cancel()
        try:
            await session.soft_mute_resume_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_timed_soft_mute_auto_resume_clears_session_without_forcing_attention(self):
        from plugins import attention as attention_mod

        session = SimpleNamespace(soft_muted=True, soft_mute_resume_task=None)
        with patch("plugins.attention.attention_service") as mock_service:
            mock_service.get_state = AsyncMock(
                return_value=AttentionState(owner_id="owner-1", mode="active")
            )
            task = asyncio.create_task(
                attention_mod._auto_resume_soft_mute(session, "owner-1", 0)
            )
            session.soft_mute_resume_task = task
            await task

        assert session.soft_muted is False
        assert session.soft_mute_resume_task is None
        mock_service.get_state.assert_awaited_once_with("owner-1")

    @pytest.mark.asyncio
    async def test_clear_soft_mute_cancels_pending_resume_task(self):
        from plugins import attention as attention_mod

        async def wait_forever():
            await asyncio.sleep(60)

        task = asyncio.create_task(wait_forever())
        session = SimpleNamespace(soft_muted=True, soft_mute_resume_task=task)

        attention_mod.clear_soft_mute_for_session(session)

        assert session.soft_muted is False
        assert session.soft_mute_resume_task is None
        assert task.cancelled() or task.cancelling()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_tool_set_mode_active_clears_live_session_soft_mute(self, tool_context):
        from plugins.attention import AttentionPlugin

        session = SimpleNamespace(
            soft_muted=True,
            processor=SimpleNamespace(force_passive=MagicMock(), force_active=MagicMock()),
        )
        fake_manager = SimpleNamespace(get_session=MagicMock(return_value=session))
        with (
            patch("api.websockets.connection.manager", fake_manager),
            patch("plugins.attention.attention_service") as mock_service,
        ):
            mock_service.set_mode = AsyncMock(
                return_value=AttentionState(owner_id="owner-1", mode="active")
            )
            await AttentionPlugin().set_mode("active")

        mock_service.set_mode.assert_awaited_once_with(
            "owner-1", "active", duration_minutes=None, source="tool"
        )
        assert session.soft_muted is False
        session.processor.force_active.assert_not_called()
        session.processor.force_passive.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_fast_path_prefers_attention_plugin_helper(self):
        from api.websockets import handlers

        plugin = SimpleNamespace(set_mode_for_identity=AsyncMock())
        with patch.object(handlers.registry, "plugins", {"attention": plugin}):
            await handlers._set_attention_mode_fast_path("owner-1", "node-1", "quiet")

        plugin.set_mode_for_identity.assert_awaited_once_with(
            "owner-1", "node-1", "quiet", source="local_command"
        )


class TestAttentionOrchestratorIntegration:
    @staticmethod
    def _fake_manager(session: SimpleNamespace) -> SimpleNamespace:
        from api.websockets.presence import LocationRef
        from core.triggers.endpoint_router import LiveEndpoint

        return SimpleNamespace(
            get_session=MagicMock(return_value=session),
            get_session_by_connection=MagicMock(return_value=session),
            list_live_endpoints=MagicMock(
                return_value=[
                    LiveEndpoint(
                        connection_id=session.connection_id,
                        node_id=getattr(session, "node_id", "node-1"),
                        capabilities=frozenset({"mic", "speaker"}),
                        location=LocationRef(),
                        last_active_at=None,
                        connected_at=0.0,
                    )
                ]
            ),
            default_connection_by_owner_id={session.owner_id: session.connection_id},
            send_voice_response=AsyncMock(),
        )

    @staticmethod
    def _instance(
        *,
        decision: str = "act",
        attention_level: str = "normal",
        sound: str = "chime",
    ) -> TriggerInstance:
        now = datetime.now(timezone.utc)
        return TriggerInstance(
            id="trg-1",
            owner_id="owner-1",
            status="claimed",
            due_at=now,
            created_at=now,
            origin_snapshot=TriggerOrigin(kind="external", source="test", event="fire"),
            action_snapshot=TriggerAction(
                decision=decision,
                message="Archive this",
                instructions="Archive it",
            ),
            attention_snapshot=AttentionPolicy(level=attention_level, sound=sound),
            delivery_snapshot=DeliveryPlan(),
            freshness_snapshot=FreshnessPolicy(),
            management={"provider": "automations", "resource_id": "trg-1"},
        )

    @pytest.mark.asyncio
    async def test_act_trigger_runs_headless_without_notification_sound(self):
        from core.turns.orchestrator import AssistantOrchestrator
        from services.events import Event, EventType

        orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
        session = SimpleNamespace(
            owner_id="owner-1",
            connection_id="conn-1",
            presence=None,
            context={"owner_id": "owner-1", "connection_id": "conn-1", "timezone": "UTC"},
        )
        fake_manager = self._fake_manager(session)
        result = TurnResult(full_response="done")
        orchestrator._run_silent_turn = AsyncMock(return_value=result)
        scheduled = []

        def schedule(runner, *_args, **_kwargs):
            scheduled.append(runner)

        orchestrator._schedule_runner = schedule

        trigger_service = SimpleNamespace(
            get_instance=AsyncMock(return_value=self._instance(decision="act")),
            mark_executing=AsyncMock(return_value=True),
            record_turn_id=AsyncMock(),
            complete_instance=AsyncMock(),
            mark_awaiting_delivery=AsyncMock(),
            suppress_instance=AsyncMock(),
            fail_instance=AsyncMock(),
        )

        with (
            patch("core.triggers.service.trigger_service", trigger_service),
            patch("api.websockets.connection.manager", fake_manager),
            patch("core.turns.orchestrator.attention_service.get_mode", AsyncMock(return_value="active")),
        ):
            await orchestrator._handle_trigger_due(
                Event(type=EventType.TRIGGER_DUE, data={"instance_id": "trg-1", "owner_id": "owner-1"})
            )
            assert len(scheduled) == 1
            fake_manager.send_voice_response.assert_not_awaited()
            await scheduled[0]()

        orchestrator._run_silent_turn.assert_awaited_once()
        trigger_service.complete_instance.assert_awaited_once_with("trg-1", result_text="done")

    @pytest.mark.asyncio
    async def test_announce_trigger_with_no_sound_does_not_emit_notification_sound(self):
        from core.turns.orchestrator import AssistantOrchestrator
        from services.events import Event, EventType

        orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
        session = SimpleNamespace(
            owner_id="owner-1",
            connection_id="conn-1",
            presence=None,
            context={"owner_id": "owner-1", "connection_id": "conn-1", "timezone": "UTC"},
            current_run_task=None,
        )
        fake_manager = self._fake_manager(session)
        scheduled_task = asyncio.Future()
        orchestrator._schedule_runner = MagicMock(return_value=scheduled_task)

        trigger_service = SimpleNamespace(
            get_instance=AsyncMock(return_value=self._instance(decision="tell", sound="none")),
            mark_executing=AsyncMock(return_value=True),
            record_turn_id=AsyncMock(),
            mark_awaiting_delivery=AsyncMock(),
            suppress_instance=AsyncMock(),
            complete_instance=AsyncMock(),
            fail_instance=AsyncMock(),
        )

        with (
            patch("core.triggers.service.trigger_service", trigger_service),
            patch("api.websockets.connection.manager", fake_manager),
            patch("core.turns.orchestrator.attention_service.get_mode", AsyncMock(return_value="active")),
        ):
            await orchestrator._handle_trigger_due(
                Event(type=EventType.TRIGGER_DUE, data={"instance_id": "trg-1", "owner_id": "owner-1"})
            )

        fake_manager.send_voice_response.assert_not_awaited()
        orchestrator._schedule_runner.assert_called_once()
        assert session.current_run_task is scheduled_task

    @pytest.mark.asyncio
    async def test_evaluate_final_speech_defers_when_paused(self):
        from core.turns.orchestrator import AssistantOrchestrator

        orchestrator = AssistantOrchestrator.__new__(AssistantOrchestrator)
        orchestrator._run_headless_turn = AsyncMock(return_value=TurnResult(full_response="Heads up"))
        orchestrator._persist_trace = AsyncMock()
        orchestrator._deliver_text = AsyncMock()

        with patch("core.turns.orchestrator.attention_service.get_mode", AsyncMock(return_value="paused")):
            outcome, _defer_retry_at = await orchestrator._run_evaluate_turn(
                owner_id="owner-1",
                session_context={"owner_id": "owner-1", "connection_id": "conn-1"},
                system_context="SYSTEM EVENT",
                sound="chime",
                turn_id="turn-1",
                attention=AttentionPolicy(level="critical"),
            )

        assert outcome == "awaiting_delivery"
        orchestrator._deliver_text.assert_not_awaited()
