import asyncio
import logging
import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

from api.websockets.models import WSResponse
from api.websockets.types import WSMessageType
from services.events import event_bus, Event, EventType
from services.perf import perf
from services.log_buffer import bind_log_context, reset_log_context
from services.database.mongodb import mongodb
from core.config import settings
from core.setup.readiness import SetupNotReadyError, require_llm_ready
from core.id import generate_id
from core.voice.stt_service import STTBackend
from core.voice.tts_service import TTSBackend
from core.voice.processor import VoiceMode
from core.turns.delivery import (
    HeadlessDelivery,
    StreamEvent,
    TurnResult,
    VoiceDelivery,
    is_no_reply,
    parse_evaluate_sentinel,
    signal_current_delivery_cancel,
)
from core.turns.execution import execute_turn
from core.turns.history import HistoryPolicy
from core.attention.service import attention_service
from core.triggers.delivery_policy import resolve_proactive_speech_delivery
from core.triggers.due_decision import resolve_trigger_due_decision
from core.triggers.endpoint_router import resolve_proactive_endpoints
from core.triggers.models import DeliveryPlan
from core.triggers.freshness import trigger_expiry_reason
from core.triggers.offer_context import assemble_offer_state, resolve_offer_defer_retry_at
from core.triggers.vocabulary import (
    DECISION_OFFER,
    DECISION_TELL,
    DELIVERY_ANNOUNCE,
    DELIVERY_SILENT,
    TRACE_EVALUATE,
    TRACE_PREFETCHED,
    TRACE_SUPPRESSED,
)
from core.llm.service import LLMService
from core.agent.agent import JarvisAgent
from core.prompts.protocol_context import build_protocol_context
from core.prompts.system_turn_context import (
    SystemTurnContext,
    build_system_routing_hint,
    build_system_turn_message,
    system_turn_context_from_trigger,
)
from services.headless_pool import HeadlessTurnPool

logger = logging.getLogger(__name__)

SESSION_FRESHNESS_SECONDS = 20.0
DEFER_RETRY_DELAY = timedelta(minutes=10)
DEFER_RETRY_JITTER = timedelta(minutes=2)
DELIVERY_RETRY_DELAY = timedelta(seconds=5)
# Loud delivery attempts for requires_ack instances before quiet-settling to delivered.
# Freshness still governs lifetime; this only stops re-claiming endpoints for retries.
# Authored today via alarm_preset (requires_ack=True); gate is the axis, not "alarm".
ACK_MAX_DELIVERY_ATTEMPTS = 6
_NOTIFICATION_SOUNDS: frozenset[str] = frozenset({"chime", "timer", "alarm"})


def _notification_sound(sound: str | None) -> str | None:
    """Return a playable notification sound, or None for silent/unknown values."""
    return sound if sound in _NOTIFICATION_SOUNDS else None


@dataclass(frozen=True, slots=True)
class TriggerSettlementOutcome:
    """Post-execution fate for a TriggerInstance after agent/delivery work."""

    kind: Literal[
        "delivered",
        "completed",
        "suppressed",
        "awaiting_delivery",
        "offer_deferred",
        "failed",
    ]
    reason: str | None = None
    result_text: str | None = None
    next_retry_at: datetime | None = None


def _next_defer_retry_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    jitter_s = random.uniform(0, DEFER_RETRY_JITTER.total_seconds())
    return now + DEFER_RETRY_DELAY + timedelta(seconds=jitter_s)


def _next_delivery_retry_at(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + DELIVERY_RETRY_DELAY


class AssistantOrchestrator:
    """
    Coordinates the voice interaction lifecycle: STT -> Agent -> TTS.
    Manages conversational state transitions and barge-in logic.
    """
    
    def __init__(
        self, 
        stt: STTBackend, 
        llm: LLMService, 
        agent: JarvisAgent, 
        tts: TTSBackend
    ):
        self.stt = stt
        self.llm = llm
        self.agent = agent
        self.tts = tts
        self.headless_pool = HeadlessTurnPool(max_concurrent=settings.AGENT_HEADLESS_CONCURRENCY)
        # Subscribe to proactive events
        event_bus.subscribe(EventType.TRIGGER_DUE, self._handle_trigger_due)
        event_bus.subscribe(EventType.TRIGGER_RETRY_AWAITING, self._handle_trigger_retry_awaiting)
        event_bus.subscribe(EventType.PROTOCOL_RUN, self._handle_protocol_run)
        event_bus.subscribe(EventType.SESSION_CONNECTED, self._handle_session_connected)
        event_bus.subscribe(EventType.ATTENTION_CHANGED, self._handle_attention_changed)
        event_bus.subscribe(EventType.VOICE_USER_START, self._handle_interruption)
        event_bus.subscribe(EventType.VOICE_WAKE, self._handle_interruption)
        event_bus.subscribe(EventType.VOICE_INTERRUPT, self._handle_interruption)
        event_bus.subscribe(EventType.VOICE_SESSION_END, self._handle_session_end)
        event_bus.subscribe(EventType.VOICE_TIMEOUT, self._handle_session_end)

    @property
    def manager(self):
        """Lazy load connection manager to avoid circular imports."""
        from api.websockets.connection import manager
        return manager

    @staticmethod
    def _presence_metadata(
        session: Any,
        *,
        owner_id: str,
        connection_id: str,
    ) -> Dict[str, Any]:
        """Snapshot the node metadata that belongs to the turn being persisted."""
        presence = getattr(session, "presence", None)
        location = getattr(presence, "location", None)
        return {
            "owner_id": owner_id,
            "connection_id": connection_id,
            "node_id": getattr(presence, "node_id", None),
            "node_label": getattr(presence, "node_label", None),
            "device_kind": getattr(presence, "device_kind", None),
            "location_ref": location.model_dump() if location else None,
        }

    @staticmethod
    def _turn_identity_metadata(session: Any, *, turn_id: str) -> Dict[str, Any]:
        """Per-turn speaker identity from a verified barge-in match (not presence)."""
        voice_turn = getattr(session, "voice_turn", None)
        if voice_turn is None or voice_turn.turn_id != turn_id:
            return {}
        meta: Dict[str, Any] = {}
        speaker_id = getattr(voice_turn, "speaker_id", None)
        if speaker_id:
            meta["speaker_id"] = speaker_id
        confidence = getattr(voice_turn, "speaker_confidence", None)
        if confidence is not None:
            meta["speaker_confidence"] = confidence
        source = getattr(voice_turn, "speaker_source", None)
        if source:
            meta["speaker_source"] = source
        return meta

    async def _handle_session_end(self, event: Event) -> None:
        """Handle session end (stop_listening tool or activity timeout). Resets to passive."""
        target_id = event.data.get("target_id") or event.data.get("session_id")
        if not target_id:
            return

        session = self.manager.get_session_by_connection(target_id)
        if not session:
            return

        logger.info(f"Session end for {target_id}. Resetting to passive.")
        session.processor.force_passive(
            reason=f"orchestrator.{event.type.value}",
            release_wake_refractory=event.type == EventType.VOICE_TIMEOUT,
        )
        
        # Notify frontend to show idle state
        await self.manager.send_voice_response(target_id, WSMessageType.STATUS, {"stage": "idle"})

    async def _handle_interruption(self, event: Event) -> None:
        """
        Handle interruption (Speech, Wake Word, or Manual Stop): cancel any active turn processing.
        """
        target_id = event.data.get("target_id") or event.data.get("session_id")
        if not target_id:
            return

        session = self.manager.get_session_by_connection(target_id)
        if not session:
            return

        # Defensive invariant: ambient speech starts are not actionable while muted.
        # Explicit wake/interrupt events still cancel active delivery.
        if (
            event.type == EventType.VOICE_USER_START
            and getattr(session, "soft_muted", False)
        ):
            logger.debug(
                "Ignoring VOICE_USER_START interruption for %s: session is soft_muted",
                target_id,
            )
            return

        if (session.current_run_task and not session.current_run_task.done()) or (
            session.accepted_input_task and not session.accepted_input_task.done()
        ):
            logger.info(f"Interruption ({event.type}) for {target_id}. Cancelling active turn.")
            # Signal this delivery attempt cooperatively first, then cancel as backstop.
            interrupted_delivery = signal_current_delivery_cancel(session, drain_queue=True)
            if session.accepted_input_task and not session.accepted_input_task.done():
                session.accepted_input_task.cancel()
            if session.current_run_task and not session.current_run_task.done():
                session.current_run_task.cancel()
            if interrupted_delivery.dropped_sentences:
                logger.info(
                    f"Barge-in: drained {interrupted_delivery.dropped_sentences} queued sentence(s) for {target_id}"
                )

            # Immediately tell frontend to stop audio playback
            stop_payload = {"reason": "interruption"}
            if interrupted_delivery.response_id:
                stop_payload["response_id"] = interrupted_delivery.response_id
            if interrupted_delivery.turn_id:
                stop_payload["turn_id"] = interrupted_delivery.turn_id
            await self.manager.send_voice_response(target_id, WSMessageType.STOP, stop_payload)

            # Reset mode now — the process_turn finally block defers to playback_end
            # when audio was sent, but playback_end may have already fired (or will see
            # turn_active=True because the cancelled task hasn't finished cleanup yet),
            # creating a race where nobody resets the mode.
            if session.processor.mode == VoiceMode.ACTIVE_AI_TURN:
                session.processor.set_mode(VoiceMode.ACTIVE_IDLE, source=f"interruption:{event.type.value}")

    async def _handle_session_connected(self, event: Event) -> None:
        """Handle a new session: wake the planner for awaiting_delivery trigger instances."""
        owner_id = event.data.get("owner_id") or event.data.get("session_id")
        if not owner_id:
            return

        await event_bus.publish(
            Event(
                type=EventType.TRIGGER_RETRY_AWAITING,
                source="orchestrator.session_connected",
                data={"owner_id": owner_id, "session_id": event.data.get("session_id")},
            )
        )

    async def _handle_attention_changed(self, event: Event) -> None:
        """Retry deferred trigger delivery when proactive interruptions resume."""
        owner_id = event.data.get("owner_id")
        state = event.data.get("state") or {}
        if not owner_id or state.get("mode") != "active":
            return

        await event_bus.publish(
            Event(
                type=EventType.TRIGGER_RETRY_AWAITING,
                source="orchestrator.attention_changed",
                data={"owner_id": owner_id},
            )
        )

    # --- Protocol run logging ---

    async def _run_and_log_protocol(
        self,
        protocol_name: str,
        owner_id: str,
        triggered_by: str,
        runner: Callable[[], Awaitable[Any]],
        turn_id: str,
    ) -> None:
        """Wrap a turn runner to log protocol execution to protocol_runs collection."""
        started_at = datetime.now(timezone.utc)
        status = "completed"
        try:
            await runner()
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            status = "failed"
            logger.exception(f"Protocol run failed: {protocol_name}")
        finally:
            try:
                await mongodb.db.protocol_runs.insert_one({
                    "protocol_name": protocol_name,
                    "owner_id": owner_id,
                    "triggered_by": triggered_by,
                    "started_at": started_at,
                    "completed_at": datetime.now(timezone.utc),
                    "status": status,
                    "turn_id": turn_id,
                })
            except Exception as e:
                logger.error(f"Failed to log protocol run: {e}")

    def _wrap_with_trigger_delivery_finalize(
        self,
        inner: Callable[[], Awaitable[Any]],
        instance_id: str,
        session: Any,
    ) -> Callable[[], Awaitable[Any]]:
        """Settle a TriggerInstance by audio delivery outcome."""
        async def _runner() -> None:
            session.last_turn_audio_sent = False
            session.last_turn_audio_completed = False
            session.last_turn_routed_tools = set()
            session.current_trigger_instance_id = instance_id
            try:
                await inner()
            finally:
                if getattr(session, "current_trigger_instance_id", None) == instance_id:
                    session.current_trigger_instance_id = None
                outcome = (
                    TriggerSettlementOutcome(kind="delivered")
                    if session.last_turn_audio_completed
                    else TriggerSettlementOutcome(
                        kind="awaiting_delivery",
                        reason="no_audio_sent",
                        next_retry_at=_next_delivery_retry_at(),
                    )
                )
                try:
                    await asyncio.shield(
                        self._settle_trigger_instance(
                            instance_id,
                            outcome,
                            session=session,
                        )
                    )
                except Exception as exc:
                    logger.error("Failed to finalize trigger %s: %s", instance_id, exc)

        return _runner

    async def _settle_trigger_instance(
        self,
        instance_id: str,
        outcome: TriggerSettlementOutcome,
        *,
        session: Any | None = None,
    ) -> bool:
        """Apply one post-execution settlement via TriggerService. Returns True if settled."""
        from core.triggers.service import trigger_service

        settled = False
        if outcome.kind == "delivered":
            settled = await trigger_service.mark_delivered(
                instance_id,
                result_text=outcome.result_text,
            )
            if settled and session is not None:
                self._record_delivered_route_carryover(session)
        elif outcome.kind == "completed":
            await trigger_service.complete_instance(
                instance_id,
                result_text=outcome.result_text,
            )
            settled = True
        elif outcome.kind == "suppressed":
            await trigger_service.suppress_instance(
                instance_id,
                reason=outcome.reason,
            )
            settled = True
        elif outcome.kind in ("awaiting_delivery", "offer_deferred"):
            settled = await trigger_service.mark_awaiting_delivery(
                instance_id,
                reason=outcome.reason or "no_target",
                next_retry_at=outcome.next_retry_at,
            )
        elif outcome.kind == "failed":
            await trigger_service.fail_instance(
                instance_id,
                reason=outcome.reason or "evaluation_failed",
            )
            settled = True
        else:
            raise ValueError(f"unsupported settlement kind: {outcome.kind}")

        if not settled and outcome.kind in ("delivered", "awaiting_delivery", "offer_deferred"):
            logger.debug(
                "Trigger %s was already settled before finalization (%s)",
                instance_id,
                outcome.kind,
            )
        return settled

    @staticmethod
    def _record_delivered_route_carryover(session: Any) -> None:
        tools = set(getattr(session, "last_turn_routed_tools", set()))
        session.last_turn_routed_tools = set()
        from core.tool_router import tool_router
        tool_router.record_route_carryover(session.connection_id, tools=tools)

    # --- Trigger handlers ---

    async def _handle_trigger_due(self, event: Event) -> None:
        """Execute a claimed TriggerInstance by routing to the correct delivery primitive."""
        from core.triggers.lifecycle import rule_allows_dispatch
        from core.triggers.service import trigger_service

        instance_id = event.data.get("instance_id")
        owner_id = event.data.get("owner_id")
        if not instance_id or not owner_id:
            return

        instance = await trigger_service.get_instance(instance_id)
        if not instance or instance.status not in ("claimed", "pending"):
            logger.debug("Trigger %s already handled (status=%s)", instance_id,
                         instance.status if instance else "missing")
            return
        if instance.status == "pending":
            claimed = await trigger_service.claim_instance(instance_id)
            if not claimed:
                logger.debug("Trigger %s was claimed before handler execution", instance_id)
                return
            instance = claimed

        if instance.rule_id:
            parent_rule = await trigger_service.get_rule(instance.rule_id)
            if parent_rule is None or not rule_allows_dispatch(parent_rule):
                await trigger_service.cancel_instance(
                    instance_id,
                    reason=(
                        "parent_rule_missing"
                        if parent_rule is None
                        else "parent_rule_paused_or_disabled"
                    ),
                )
                return

        attention_mode = await attention_service.get_mode(owner_id)
        decision = resolve_trigger_due_decision(
            instance=instance,
            attention_mode=attention_mode,
        )

        if decision.force_delivery_reason:
            logger.info(
                "Trigger %s reached freshness deadline; forcing delivery: %s",
                instance_id, decision.force_delivery_reason,
            )

        if decision.kind == "expire":
            logger.info(
                "Trigger %s expired before delivery: %s",
                instance_id, decision.reason,
            )
            await trigger_service.expire_instance(instance_id, reason=decision.reason)
            return
        if decision.kind == "awaiting_delivery":
            logger.info(
                "Trigger %s deferred: attention=%s reason=%s",
                instance_id, attention_mode, decision.reason,
            )
            await trigger_service.mark_awaiting_delivery(instance_id, reason=decision.reason)
            return
        if decision.kind == "suppress":
            logger.info(
                "Trigger %s suppressed: attention=%s reason=%s",
                instance_id, attention_mode, decision.reason,
            )
            await trigger_service.suppress_instance(instance_id, reason=decision.reason)
            return
        if decision.kind == "complete":
            await trigger_service.complete_instance(instance_id)
            return

        # Attention gate passed; transition to executing before routing/session resolution.
        if not await trigger_service.mark_executing(instance_id):
            logger.debug("Trigger %s was settled before execution", instance_id)
            return

        routing = resolve_proactive_endpoints(
            delivery=instance.delivery_snapshot,
            endpoints=self.manager.list_live_endpoints(owner_id),
        )
        if not routing.target:
            logger.info(
                "No proactive endpoint for %s; marking trigger %s awaiting_delivery (%s)",
                owner_id,
                instance_id,
                routing.reason,
            )
            await trigger_service.mark_awaiting_delivery(instance_id, reason=routing.reason)
            return

        session = self.manager.get_session_by_connection(routing.target.connection_id)
        if not session:
            await trigger_service.mark_awaiting_delivery(instance_id, reason="target_offline")
            return

        action = instance.action_snapshot
        delivery_resolution = decision.delivery_resolution
        force_delivery_reason = decision.force_delivery_reason
        turn_id = generate_id("turn-")
        origin: Dict[str, Any] = {
            "trigger_source": "trigger",
            "instance_id": instance_id,
            "decision": action.decision,
        }
        if instance.rule_id:
            origin["rule_id"] = instance.rule_id
        await trigger_service.record_turn_id(instance_id, turn_id)
        presence_metadata = self._presence_metadata(
            session,
            owner_id=session.owner_id,
            connection_id=session.connection_id,
        )

        protocol_name = action.protocol_name
        protocol_context = ""
        if protocol_name:
            tz_name = session.context.get("timezone", "UTC")
            protocol_context = await build_protocol_context(protocol_name, owner_id, tz_name)

        if protocol_name and not protocol_context:
            logger.warning("Protocol '%s' not found for instance %s", protocol_name, instance_id)
            await trigger_service.fail_instance(instance_id, reason=f"protocol '{protocol_name}' not found")
            return

        turn_ctx = system_turn_context_from_trigger(
            instance,
            mode=delivery_resolution.delivery_tag,
            protocol_context=protocol_context,
        )
        if turn_ctx.decision == DECISION_OFFER:
            tz_name = session.context.get("timezone", "UTC")
            turn_ctx = replace(
                turn_ctx,
                current_state=await assemble_offer_state(instance, timezone_name=tz_name),
            )
        system_context = build_system_turn_message(turn_ctx)
        routing_hint = build_system_routing_hint(turn_ctx)
        trigger_decision = turn_ctx.decision
        is_protocol = bool(protocol_name and protocol_context)
        sound = (instance.attention_snapshot.sound or "chime")

        if delivery_resolution.presentation == "never":
            await self._dispatch_act_trigger(
                instance_id=instance_id,
                owner_id=owner_id,
                session=session,
                system_context=system_context,
                routing_hint=routing_hint,
                turn_id=turn_id,
                origin=origin,
                presence_metadata=presence_metadata,
                trigger_decision=trigger_decision,
                protocol_name=protocol_name,
                is_protocol=is_protocol,
            )
        elif delivery_resolution.presentation == "if_content":
            await self._dispatch_offer_trigger(
                instance=instance,
                instance_id=instance_id,
                owner_id=owner_id,
                session=session,
                system_context=system_context,
                routing_hint=routing_hint,
                turn_id=turn_id,
                origin=origin,
                presence_metadata=presence_metadata,
                trigger_decision=trigger_decision,
                protocol_name=protocol_name,
                is_protocol=is_protocol,
                sound=sound,
                force_delivery_text=action.message if force_delivery_reason else None,
                is_offer=turn_ctx.decision == DECISION_OFFER,
            )
        elif action.decision == DECISION_TELL:
            await self._dispatch_tell_trigger(
                instance_id=instance_id,
                owner_id=owner_id,
                session=session,
                system_context=system_context,
                routing_hint=routing_hint,
                turn_id=turn_id,
                origin=origin,
                trigger_decision=trigger_decision,
                protocol_name=protocol_name,
                is_protocol=is_protocol,
                sound=sound,
            )
        else:
            logger.warning("Trigger %s has unsupported decision %s", instance_id, action.decision)
            await trigger_service.fail_instance(instance_id, reason=f"unsupported decision '{action.decision}'")

    async def _dispatch_act_trigger(
        self,
        *,
        instance_id: str,
        owner_id: str,
        session: Any,
        system_context: str,
        routing_hint: str | None,
        turn_id: str,
        origin: Dict[str, Any],
        presence_metadata: Dict[str, Any],
        trigger_decision: str,
        protocol_name: str | None,
        is_protocol: bool,
    ) -> None:
        async def runner() -> None:
            try:
                result = await self._run_silent_turn(
                    owner_id=owner_id,
                    session_context=session.context,
                    system_context=system_context,
                    turn_id=turn_id,
                    origin=origin,
                    routing_hint=routing_hint,
                    presence_metadata=presence_metadata,
                    trigger_decision=trigger_decision,
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._settle_trigger_instance(
                        instance_id,
                        TriggerSettlementOutcome(
                            kind="awaiting_delivery",
                            reason="execution_cancelled",
                            next_retry_at=_next_delivery_retry_at(),
                        ),
                    )
                )
                raise
            except Exception as exc:
                await self._settle_trigger_instance(
                    instance_id,
                    TriggerSettlementOutcome(kind="failed", reason=str(exc)[:500]),
                )
                raise
            if result.runtime_error:
                await self._settle_trigger_instance(
                    instance_id,
                    TriggerSettlementOutcome(kind="failed", reason="runtime_error"),
                )
                return
            await self._settle_trigger_instance(
                instance_id,
                TriggerSettlementOutcome(
                    kind="completed",
                    result_text=result.full_response.strip() or None,
                ),
            )

        self._schedule_runner(
            runner, protocol_name, owner_id, "trigger", is_protocol,
            headless=True, turn_id=turn_id,
        )

    async def _dispatch_offer_trigger(
        self,
        *,
        instance: Any,
        instance_id: str,
        owner_id: str,
        session: Any,
        system_context: str,
        routing_hint: str | None,
        turn_id: str,
        origin: Dict[str, Any],
        presence_metadata: Dict[str, Any],
        trigger_decision: str,
        protocol_name: str | None,
        is_protocol: bool,
        sound: str,
        force_delivery_text: str | None,
        is_offer: bool,
    ) -> None:
        suppressed_reason = "offer_no_reply" if is_offer else "evaluate_no_reply"

        async def runner() -> None:
            session.last_turn_routed_tools = set()
            try:
                outcome, defer_retry_at = await self._run_evaluate_turn(
                    owner_id=owner_id,
                    session_context=session.context,
                    system_context=system_context,
                    sound=sound,
                    turn_id=turn_id,
                    origin=origin,
                    routing_hint=routing_hint,
                    attention=instance.attention_snapshot,
                    presence_metadata=presence_metadata,
                    force_delivery_text=force_delivery_text,
                    trigger_decision=trigger_decision,
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._settle_trigger_instance(
                        instance_id,
                        TriggerSettlementOutcome(
                            kind="awaiting_delivery",
                            reason="execution_cancelled",
                            next_retry_at=_next_delivery_retry_at(),
                        ),
                    )
                )
                raise
            except Exception as exc:
                await self._settle_trigger_instance(
                    instance_id,
                    TriggerSettlementOutcome(kind="failed", reason=str(exc)[:500]),
                )
                raise

            if outcome == TRACE_SUPPRESSED:
                settlement = TriggerSettlementOutcome(
                    kind="suppressed",
                    reason=suppressed_reason,
                )
            elif outcome == "delivered":
                settlement = TriggerSettlementOutcome(kind="delivered")
            elif outcome == "awaiting_delivery":
                settlement = TriggerSettlementOutcome(
                    kind="awaiting_delivery",
                    reason="no_audio_sent",
                    next_retry_at=_next_delivery_retry_at(),
                )
            elif outcome == "offer_deferred":
                settlement = TriggerSettlementOutcome(
                    kind="offer_deferred",
                    reason="offer_deferred",
                    next_retry_at=resolve_offer_defer_retry_at(
                        defer_retry_at,
                        instance,
                        fallback=_next_defer_retry_at(),
                    ),
                )
            else:
                settlement = TriggerSettlementOutcome(
                    kind="failed",
                    reason="evaluation_failed",
                )
            await self._settle_trigger_instance(
                instance_id,
                settlement,
                session=session,
            )

        self._schedule_runner(
            runner, protocol_name, owner_id, "trigger", is_protocol,
            headless=True, turn_id=turn_id,
        )

    async def _dispatch_tell_trigger(
        self,
        *,
        instance_id: str,
        owner_id: str,
        session: Any,
        system_context: str,
        routing_hint: str | None,
        turn_id: str,
        origin: Dict[str, Any],
        trigger_decision: str,
        protocol_name: str | None,
        is_protocol: bool,
        sound: str,
    ) -> None:
        used_cache = False
        notification_sound = _notification_sound(sound)
        connection_id = session.connection_id
        if is_protocol and protocol_name:
            used_cache = await self._try_prefetched_delivery(
                session=session,
                trigger_data={"owner_id": owner_id},
                sound=notification_sound,
                protocol_name=protocol_name,
                triggered_by="trigger",
                instance_id=instance_id,
                turn_id=turn_id,
                origin=origin,
            )
        if used_cache:
            return

        if notification_sound:
            await self.manager.send_voice_response(
                connection_id, WSMessageType.NOTIFICATION_SOUND, {"sound": notification_sound}
            )
            await asyncio.sleep(0.5)
        runner = self._wrap_with_trigger_delivery_finalize(
            lambda cid=connection_id: self.process_turn(
                connection_id=cid,
                audio_bytes=None,
                system_context=system_context,
                source="system",
                delivery=DELIVERY_ANNOUNCE,
                turn_id=turn_id,
                origin=origin,
                routing_hint=routing_hint,
                trigger_decision=trigger_decision,
            ),
            instance_id, session,
        )
        self._set_current_run_task(
            session,
            self._schedule_runner(
                runner, protocol_name, owner_id, "trigger", is_protocol,
                headless=False, turn_id=turn_id,
            ),
        )

    async def _handle_trigger_retry_awaiting(self, event: Event) -> None:
        """Re-execute awaiting_delivery trigger instances when a session becomes available."""
        from core.triggers.service import trigger_service

        owner_id = event.data.get("owner_id")
        if not owner_id:
            return

        if not self.manager.list_owner_sessions(owner_id):
            return

        now = datetime.now(timezone.utc)
        instances = await trigger_service.get_awaiting_delivery(
            owner_id,
            retry_due_at=now,
            include_unscheduled=not bool(event.data.get("retry_due_only")),
        )
        if not instances:
            return

        instances = await trigger_service.dedupe_awaiting_for_retry(instances)
        if not instances:
            return

        logger.info(
            "Retrying %d awaiting_delivery trigger(s) for %s",
            len(instances), owner_id,
        )
        for instance in instances:
            expiry_reason = trigger_expiry_reason(instance)
            if expiry_reason:
                logger.info(
                    "Expiring stale trigger %s instead of retrying delivery: %s",
                    instance.id, expiry_reason,
                )
                await trigger_service.expire_instance(instance.id, reason=expiry_reason)
                continue

            attention = instance.attention_snapshot
            requires_ack = bool(getattr(attention, "requires_ack", False))
            attempts = len(instance.turn_ids or [])
            if requires_ack and attempts >= ACK_MAX_DELIVERY_ATTEMPTS:
                # Stop loud retries; keep ackable via delivered + requires_ack.
                if await trigger_service.claim_awaiting_instance(instance.id):
                    await trigger_service.mark_delivered(
                        instance.id,
                        result_text="ring_budget_exhausted",
                    )
                    logger.info(
                        "Ack delivery budget exhausted for %s after %d attempt(s); quiet-settled to delivered",
                        instance.id,
                        attempts,
                    )
                continue

            # Atomically move awaiting_delivery → claimed so the handler treats it as a fresh dispatch.
            if await trigger_service.claim_awaiting_instance(instance.id):
                await event_bus.publish(
                    Event(
                        type=EventType.TRIGGER_DUE,
                        source="retry_awaiting",
                        data={"instance_id": instance.id, "owner_id": owner_id},
                    )
                )

    # --- Event handlers ---

    def _schedule_runner(
        self,
        runner: Callable[[], Awaitable[Any]],
        protocol_name: Optional[str],
        owner_id: str,
        triggered_by: str,
        is_protocol: bool,
        *,
        headless: bool,
        turn_id: str,
    ) -> asyncio.Task:
        """Wrap in protocol logging if needed, then schedule via the right path."""
        if is_protocol and protocol_name:
            wrapped = self._run_and_log_protocol(
                protocol_name, owner_id, triggered_by, runner, turn_id=turn_id,
            )
        else:
            wrapped = runner()
        return self.headless_pool.schedule(wrapped) if headless else asyncio.create_task(wrapped)

    @staticmethod
    def _set_current_run_task(session: Any, task: asyncio.Task) -> asyncio.Task:
        session.current_run_task = task

        def _clear_current_run_task(done_task: asyncio.Task) -> None:
            if session.current_run_task is done_task:
                session.current_run_task = None

        task.add_done_callback(_clear_current_run_task)
        return task

    async def _handle_protocol_run(self, event: Event) -> None:
        """Handle on-demand protocol execution triggered by the run tool."""
        owner_id = event.data.get("owner_id")
        protocol_name = event.data.get("protocol_name")

        if not owner_id or not protocol_name:
            return

        connection_id = event.data.get("connection_id")
        if connection_id:
            session = self.manager.get_session_by_connection(connection_id)
        else:
            session = self.manager.get_default_session_for_owner(owner_id)
        if not session:
            logger.warning(
                "No live connection for protocol run '%s' (owner=%s connection=%s).",
                protocol_name,
                owner_id,
                connection_id,
            )
            return

        tz_name = session.context.get("timezone", "UTC")
        protocol_context = await build_protocol_context(protocol_name, owner_id, tz_name)
        if not protocol_context:
            logger.warning(f"Protocol '{protocol_name}' not found for owner {owner_id}.")
            return

        system_context = build_system_turn_message(SystemTurnContext(
            message=f'User requested protocol "{protocol_name}".',
            mode=DELIVERY_ANNOUNCE,
            protocol_context=protocol_context,
        ))

        turn_id = generate_id("turn-")
        origin: Dict[str, Any] = {"trigger_source": "manual", "protocol_name": protocol_name}

        turn_kwargs = dict(
            connection_id=session.connection_id,
            audio_bytes=None,
            system_context=system_context,
            source="system",
            delivery=DELIVERY_ANNOUNCE,
            turn_id=turn_id,
            origin=origin,
            routing_hint=protocol_context,
        )
        self._set_current_run_task(
            session,
            asyncio.create_task(
                self._run_and_log_protocol(
                    protocol_name, owner_id, "manual",
                    lambda: self.process_turn(**turn_kwargs),
                    turn_id=turn_id,
                )
            ),
        )

    # --- Headless turn helpers (Phase 9a) ---

    async def _run_headless_turn(
        self,
        *,
        session_context: Dict[str, Any],
        system_context: str,
        routing_hint: Optional[str] = None,
        history_policy: HistoryPolicy = "headless_minimal",
        trigger_decision: str | None = None,
    ) -> TurnResult:
        """Run an agent turn with no user-facing output. Semaphore-gated.

        Used by silent automations, the evaluate pass, and prefetch. Bypasses
        session.turn_lock, set_mode, and WS status messages. Tool side effects
        still execute. Returns the raw `TurnResult`; callers decide whether to
        write the trace.
        """
        try:
            require_llm_ready()
        except SetupNotReadyError as exc:
            logger.info("Skipping headless turn while Jarvis Host setup is incomplete: %s", exc)
            return TurnResult()
        result = TurnResult()
        owner_id = str(session_context["owner_id"])
        connection_id = str(session_context["connection_id"])
        async with self.headless_pool.semaphore:
            await self._execute_turn(
                transcript=system_context,
                source="system",
                connection_id=connection_id,
                owner_id=owner_id,
                session_context=session_context,
                text_input=False,
                attachments=None,
                delivery=HeadlessDelivery(),
                result=result,
                routing_hint=routing_hint,
                history_policy=history_policy,
                trigger_decision=trigger_decision,
            )
        return result

    async def _run_silent_turn(
        self,
        *,
        owner_id: str,
        session_context: Dict[str, Any],
        system_context: str,
        turn_id: str,
        origin: Optional[Dict[str, Any]] = None,
        routing_hint: Optional[str] = None,
        presence_metadata: Optional[Dict[str, Any]] = None,
        trigger_decision: str | None = None,
    ) -> TurnResult:
        """Run a proactive trigger without user-facing sound or speech."""
        result = await self._run_headless_turn(
            session_context=session_context,
            system_context=system_context,
            routing_hint=routing_hint,
            trigger_decision=trigger_decision,
        )
        await self._persist_trace(
            owner_id, "system", result.turn_trace,
            turn_id=turn_id, delivery=DELIVERY_SILENT, origin=origin,
            presence_metadata=presence_metadata,
        )
        return result

    async def _run_evaluate_turn(
        self,
        *,
        owner_id: str,
        session_context: Dict[str, Any],
        system_context: str,
        sound: str,
        turn_id: str,
        attention: Any,
        origin: Optional[Dict[str, Any]] = None,
        routing_hint: Optional[str] = None,
        presence_metadata: Optional[Dict[str, Any]] = None,
        force_delivery_text: str | None = None,
        trigger_decision: str | None = None,
    ) -> tuple[
        Literal["suppressed", "delivered", "awaiting_delivery", "offer_deferred", "failed"],
        datetime | None,
    ]:
        """Headless evaluation pass. Speaks only if the agent does not respond NO_REPLY or DEFER."""
        try:
            result = await self._run_headless_turn(
                session_context=session_context,
                system_context=system_context,
                routing_hint=routing_hint,
                history_policy="proactive_bounded",
                trigger_decision=trigger_decision,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"evaluate headless turn crashed for {owner_id}; suppressing delivery")
            return "failed", None

        if result.runtime_error:
            await self._persist_trace(
                owner_id, "system", result.turn_trace,
                turn_id=turn_id, delivery=TRACE_EVALUATE, origin=origin,
                presence_metadata=presence_metadata,
            )
            return "failed", None

        response_text = result.full_response.strip()

        sentinel = parse_evaluate_sentinel(result.full_response)
        if sentinel is not None:
            if force_delivery_text:
                response_text = force_delivery_text.strip()
            else:
                if sentinel.action == "defer":
                    logger.debug(
                        "evaluate: agent deferred offer for %s%s",
                        owner_id,
                        f" until {sentinel.retry_at.isoformat()}" if sentinel.retry_at else "",
                    )
                else:
                    logger.debug(f"evaluate: agent responded NO_REPLY for {owner_id}, skipping delivery")
                await self._persist_trace(
                    owner_id, "system", result.turn_trace,
                    turn_id=turn_id, delivery=TRACE_SUPPRESSED, origin=origin,
                    presence_metadata=presence_metadata,
                )
                if sentinel.action == "suppress":
                    return TRACE_SUPPRESSED, None
                return "offer_deferred", sentinel.retry_at

        if not response_text:
            if force_delivery_text is not None:
                return "failed", None
            return "offer_deferred", None

        await self._persist_trace(
            owner_id, "system", result.turn_trace,
            turn_id=turn_id, delivery=TRACE_EVALUATE, origin=origin,
            presence_metadata=presence_metadata,
        )

        attention_mode = await attention_service.get_mode(owner_id)
        speech_resolution = resolve_proactive_speech_delivery(
            attention_mode=attention_mode,
            attention=attention,
            delivery_tag=TRACE_EVALUATE,
        )
        if speech_resolution.blocked_result:
            logger.info(
                "evaluate speech deferred for %s: attention=%s reason=%s",
                owner_id, attention_mode, speech_resolution.reason,
            )
            return "awaiting_delivery", None

        routing = resolve_proactive_endpoints(
            delivery=DeliveryPlan(),
            endpoints=self.manager.list_live_endpoints(owner_id),
        )
        if not routing.target:
            return "awaiting_delivery", None

        await self._deliver_text(
            routing.target.connection_id,
            response_text,
            sound,
            delivery=TRACE_EVALUATE,
            persist=False,
        )
        live_session = self.manager.get_session_by_connection(routing.target.connection_id)
        if live_session:
            live_session.last_turn_routed_tools = set(result.routed_tools)
        outcome = (
            "delivered"
            if live_session and live_session.last_turn_audio_completed
            else "awaiting_delivery"
        )
        return outcome, None

    # --- Prefetch consumption (Phase 9c) ---

    # Tolerance for fire_time drift between prefetch capture and live trigger.
    _PREFETCH_FIRE_TOLERANCE = timedelta(seconds=60)

    async def _try_prefetched_delivery(
        self,
        *,
        session: Any,
        trigger_data: Dict[str, Any],
        sound: Optional[str],
        protocol_name: str,
        triggered_by: str,
        instance_id: Optional[str] = None,
        turn_id: str,
        origin: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Atomically claim + deliver a prefetched briefing if one is valid.

        Returns True if delivery started (handler should skip live execution).
        Returns False on any miss / reject so the caller falls through to the
        live `process_turn` path. Never raises — cache lookup failures degrade
        to the live path.
        """
        owner_id = trigger_data.get("owner_id")
        if not owner_id:
            return False

        data_instance_id = trigger_data.get("instance_id")
        if instance_id or data_instance_id:
            source = "trigger"
            trigger_id = instance_id or data_instance_id
        else:
            source = "automation"
            trigger_id = self._automation_trigger_id(trigger_data)
        if not trigger_id:
            return False

        key = {"source": source, "trigger_id": trigger_id, "protocol_name": protocol_name}
        now = datetime.now(timezone.utc)
        try:
            # Atomic claim prevents double-delivery if this handler is racily re-invoked.
            doc = await mongodb.db.prefetched_results.find_one_and_delete(key)
        except Exception as e:
            logger.warning("Prefetch cache lookup failed, falling back to live: %s", e)
            return False

        if not doc or doc.get("status") != "ready":
            return False
        text = (doc.get("text") or "").strip()
        if not text:
            return False
        expires_at = doc.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at < now:
            logger.debug("Prefetch hit expired for %s/%s", source, trigger_id)
            return False
        stored_fire = doc.get("fire_time")
        if isinstance(stored_fire, datetime):
            live_fire = self._extract_live_fire_time(trigger_data, now)
            if abs(stored_fire - live_fire) > self._PREFETCH_FIRE_TOLERANCE:
                logger.debug(
                    "Prefetch fire_time drift (%s vs %s) for %s/%s; skipping cache",
                    stored_fire.isoformat(), live_fire.isoformat(), source, trigger_id,
                )
                return False

        # Pair VoiceDelivery's perf.end("turn_latency") in _tts_worker so the
        # sub-second cache-hit win is measurable and no unstarted-span warning fires.
        connection_id = getattr(session, "connection_id", owner_id)
        owner_id = getattr(session, "owner_id", owner_id)
        perf_origin = {k: v for k, v in (origin or {}).items() if v is not None}
        perf.start(
            "turn_latency", connection_id,
            turn_id=turn_id, source="system", scenario=TRACE_PREFETCHED,
            owner_id=owner_id, connection_id=connection_id, **perf_origin,
        )

        async def _runner() -> None:
            with perf.context(
                turn_id=turn_id,
                source="system",
                scenario=TRACE_PREFETCHED,
                owner_id=owner_id,
                connection_id=connection_id,
                **perf_origin,
            ):
                await self._deliver_text(
                    connection_id,
                    text,
                    sound,
                    delivery=TRACE_PREFETCHED,
                    turn_id=turn_id,
                    origin=origin,
                )

        # Settle the trigger instance on audio outcome if we have one.
        _instance_id = instance_id or trigger_data.get("instance_id")
        if _instance_id:
            runner = self._wrap_with_trigger_delivery_finalize(_runner, _instance_id, session)
        else:
            runner = _runner
        self._set_current_run_task(
            session,
            self._schedule_runner(
                runner, protocol_name, owner_id, triggered_by, is_protocol=True,
                headless=False, turn_id=turn_id,
            ),
        )
        logger.info(
            "Prefetch hit: %s/%s protocol=%s (%d chars cached)",
            source, trigger_id, protocol_name, len(text),
        )
        return True

    @staticmethod
    def _automation_trigger_id(trigger_data: Dict[str, Any]) -> str:
        """Reconstruct automation cache key from event data.

        Automations don't carry instance ids; the prefetch cache keys them as
        "{rule_id}:{item_id}" to match `iter_upcoming_protocol_fires`.
        """
        rule_id = trigger_data.get("rule_id")
        item_id = trigger_data.get("item_id")
        if rule_id and item_id:
            return f"{rule_id}:{item_id}"
        return ""

    @staticmethod
    def _extract_live_fire_time(
        trigger_data: Dict[str, Any], now: datetime
    ) -> datetime:
        """Best-effort fire_time for drift comparison. Falls back to now."""
        for key in ("trigger_time", "fire_time"):
            ts = trigger_data.get(key)
            if isinstance(ts, datetime):
                return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return now

    async def _deliver_text(
        self,
        connection_id: str,
        text: str,
        sound: Optional[str],
        *,
        delivery: str = TRACE_EVALUATE,
        persist: bool = True,
        turn_id: Optional[str] = None,
        origin: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Speak pre-generated text through VoiceDelivery without rerunning the agent.

        `persist=True` writes a single `text_only` row — used by prefetched cache
        hits. Callers that already persisted the full trace pass `persist=False`.
        """
        session = self.manager.get_session_by_connection(connection_id)
        if not session:
            logger.warning("No live session for _deliver_text: %s", connection_id)
            return
        owner_id = session.owner_id
        presence_metadata = self._presence_metadata(
            session,
            owner_id=owner_id,
            connection_id=connection_id,
        )

        notification_sound = _notification_sound(sound)
        if notification_sound:
            await self.manager.send_voice_response(
                connection_id, WSMessageType.NOTIFICATION_SOUND, {"sound": notification_sound}
            )
            await asyncio.sleep(0.5)

        _turn_id = turn_id or generate_id("turn-")
        log_token = bind_log_context(
            turn_id=_turn_id,
            instance_id=(origin or {}).get("instance_id"),
            task_id=(origin or {}).get("task_id"),
            node_id=presence_metadata.get("node_id"),
        )
        voice: Optional[VoiceDelivery] = None
        try:
            async with session.turn_lock:
                session.processor.set_mode(VoiceMode.ACTIVE_AI_TURN, source="orchestrator.deliver_text")

                voice = VoiceDelivery(
                    session, self.manager, self.tts,
                    session_id=connection_id, turn_id=_turn_id, produce_audio=True,
                )
                await voice.start()
                try:
                    await voice.on_stream(StreamEvent(tag="text", content=text))
                    await voice.on_stream(StreamEvent(tag="final_text"))
                finally:
                    await voice.aclose()

                if persist:
                    await self._persist_trace(
                        owner_id, "system",
                        [("assistant", text, {"turn_type": "text_only"})],
                        turn_id=_turn_id, delivery=delivery, origin=origin,
                        presence_metadata=presence_metadata,
                    )
        finally:
            if voice is None or not voice.first_audio_sent:
                session.processor.set_mode(
                    VoiceMode.ACTIVE_IDLE, source="orchestrator.deliver_text_finally",
                )
            reset_log_context(log_token)

    async def _persist_trace(
        self,
        owner_id: str,
        source: str,
        trace: list,
        *,
        turn_id: str,
        delivery: Optional[str] = None,
        origin: Optional[Dict[str, Any]] = None,
        skip_initial_user: bool = False,
        presence_metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a turn trace to MongoDB.

        `turn_id` groups all rows in this batch — the universal key for any turn.
        `delivery` tags rows for read-side filtering (announce/silent/suppressed/etc).
        `origin` carries audit fields (rule_id, rule_name, trigger_source, etc).
        """
        presence_meta: Dict[str, Any] = presence_metadata or {}
        for index, entry in enumerate(trace):
            role, content = entry[0], entry[1]
            if skip_initial_user and index == 0 and role == "user":
                continue
            meta_src = entry[2] if len(entry) > 2 else {}
            meta = dict(meta_src or {})
            for k, v in presence_meta.items():
                if v is not None:
                    meta.setdefault(k, v)
            meta["turn_id"] = turn_id
            if delivery:
                meta["delivery"] = delivery
            if origin:
                for k, v in origin.items():
                    if v is not None:
                        meta.setdefault(k, v)
            try:
                await mongodb.store_message(owner_id, role, content, source=source, metadata=meta)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("turn trace persist failed: %s", e)
        if trace:
            from core.activity_events import publish_activity_changed

            await publish_activity_changed(owner_id)

    async def process_turn(
        self,
        connection_id: str,
        audio_bytes: Optional[bytes],
        text: Optional[str] = None,
        system_context: Optional[str] = None,
        source: Literal["user", "system"] = "user",
        attachments: Optional[List[Dict[str, Any]]] = None,
        delivery: Optional[str] = None,
        turn_id: Optional[str] = None,
        origin: Optional[Dict[str, Any]] = None,
        routing_hint: Optional[str] = None,
        trigger_decision: Optional[str] = None,
    ) -> None:
        """Execute a full turn: Transcribe (if voice), Think, and optionally Speak."""
        session = self.manager.get_session_by_connection(connection_id)
        if not session:
            logger.warning("No live session for connection %s, skipping turn.", connection_id)
            return

        owner_id = session.owner_id
        if source == "user":
            self.manager.record_user_turn_activity(connection_id)
        presence_metadata = self._presence_metadata(
            session,
            owner_id=owner_id,
            connection_id=connection_id,
        )
        _turn_id = turn_id or generate_id("turn-")
        turn_identity = self._turn_identity_metadata(session, turn_id=_turn_id)
        log_token = bind_log_context(
            turn_id=_turn_id,
            instance_id=(origin or {}).get("instance_id"),
            task_id=(origin or {}).get("task_id"),
            node_id=presence_metadata.get("node_id"),
        )

        scenario = "voice" if audio_bytes is not None else ("text" if text is not None else "system")
        perf_origin = {k: v for k, v in (origin or {}).items() if v is not None}
        perf_token = perf.bind_context(
            turn_id=_turn_id,
            source=source,
            scenario=scenario,
            owner_id=owner_id,
            connection_id=connection_id,
            node_id=presence_metadata.get("node_id"),
            node_label=presence_metadata.get("node_label"),
            location_ref=presence_metadata.get("location_ref"),
            **perf_origin,
        )
        perf.log(
            "process_turn_received",
            session=connection_id,
            audio_bytes=len(audio_bytes or b""),
            text_chars=len(text or ""),
            has_system_context=bool(system_context),
            attachment_count=len(attachments or []),
            delivery=delivery,
        )

        produce_audio = audio_bytes is not None or source == "system"
        transcript: str = ""
        voice: Optional[VoiceDelivery] = None
        result = TurnResult()
        persisted = False
        user_row_persisted = False
        turn_cancelled = False
        fast_recovery_cancelled = False
        turn_lock_acquired = False
        # Set only on success-path persist; finally uses this to close listening.
        no_reply = False

        def clear_voice_turn_if_current() -> None:
            if session.voice_turn is not None and session.voice_turn.turn_id == _turn_id:
                session.voice_turn = None

        try:
            perf.start("turn_lock_wait", connection_id)
            async with session.turn_lock:
                turn_lock_acquired = True
                perf.end("turn_lock_wait", connection_id)
                if produce_audio:
                    session.processor.set_mode(VoiceMode.ACTIVE_AI_TURN, source="orchestrator.turn_start")

                # --- Ingest ---
                if audio_bytes:
                    # WebSocket voice turns start this span at speech detection so it
                    # includes endpointing and streaming STT finalization.
                    if turn_id is None:
                        perf.start("turn_latency", connection_id)

                    await self.manager.send_voice_response(connection_id, WSMessageType.STATUS, {"stage": "transcribing"})
                    if text is not None:
                        transcript = text.strip()
                        transcript_source = "streaming"
                    else:
                        transcript = ""
                        transcript_source = "missing_streaming_text"

                    if not transcript:
                        logger.debug(f"No transcript for {connection_id}")
                        perf.log(
                            "transcript_empty",
                            session=connection_id,
                            transcript_source=transcript_source,
                            audio_bytes=len(audio_bytes),
                        )
                        await self.manager.send_voice_response(connection_id, WSMessageType.STATUS, {"stage": "listening"})
                        return

                    logger.info(f"Transcript [{connection_id}]: {transcript}")
                    perf.log(
                        "transcript_ready",
                        session=connection_id,
                        transcript_source=transcript_source,
                        transcript_chars=len(transcript),
                        audio_bytes=len(audio_bytes),
                    )

                    await self.manager.send_voice_response(connection_id, WSMessageType.SPEECH_END, {"is_speech": False})
                    logger.info("Transcript send: turn_id=%s text=%r", _turn_id, transcript)
                    transcript_data: dict[str, str] = {
                        "text": transcript,
                        "turn_id": _turn_id,
                    }
                    await self.manager.send_voice_response(
                        connection_id,
                        WSMessageType.TRANSCRIPT,
                        transcript_data,
                        message_id=_turn_id,
                    )
                elif text is not None:
                    transcript = text or ""
                    logger.info(f"Text Input [{connection_id}]: {transcript or '(attachment only)'}")
                    perf.log("transcript_ready", session=connection_id, transcript_source="text", transcript_chars=len(transcript))
                elif system_context:
                    transcript = system_context
                    logger.debug(f"System Turn [{connection_id}]: {transcript}")
                    perf.log("transcript_ready", session=connection_id, transcript_source="system", transcript_chars=len(transcript))
                else:
                    return

                if source == "user":
                    user_meta = {"turn_status": "pending"}
                    for key, value in presence_metadata.items():
                        if value is not None:
                            user_meta[key] = value
                    for key, value in turn_identity.items():
                        if value is not None:
                            user_meta[key] = value
                    await mongodb.upsert_user_turn(
                        owner_id,
                        _turn_id,
                        transcript,
                        metadata=user_meta,
                    )
                    user_row_persisted = True

                if connection_id.startswith("test-"):
                    logger.info(f"Skipping Agent for test session: {connection_id}")
                    return

                # --- Deliver ---
                await self.manager.send_voice_response(connection_id, WSMessageType.STATUS, {"stage": "thinking"})
                voice = VoiceDelivery(
                    session, self.manager, self.tts,
                    session_id=connection_id, turn_id=_turn_id, produce_audio=produce_audio,
                )
                await voice.start()
                perf.log(
                    "delivery_started",
                    session=connection_id,
                    produce_audio=produce_audio,
                    response_id=voice.response_id,
                )
                try:
                    execute_context = dict(session.context)
                    execute_context.update(turn_identity)
                    await self._execute_turn(
                        transcript,
                        source=source,
                        connection_id=connection_id,
                        owner_id=owner_id,
                        session_context=execute_context,
                        text_input=text is not None and audio_bytes is None,
                        attachments=attachments,
                        delivery=voice,
                        result=result,
                        routing_hint=routing_hint,
                        current_turn_id=_turn_id,
                        trigger_decision=trigger_decision,
                    )
                    if source == "system":
                        session.last_turn_routed_tools = set(result.routed_tools)
                except SetupNotReadyError as exc:
                    if source == "user":
                        await self.manager.send_message(
                            connection_id,
                            WSResponse(
                                message_id=generate_id("setup-"),
                                type=WSMessageType.ERROR,
                                data={
                                    "code": exc.code,
                                    "message": str(exc),
                                    "next_action": exc.next_action,
                                    "setup_required": True,
                                },
                            ),
                        )
                    return
                except asyncio.CancelledError as exc:
                    turn_cancelled = True
                    fast_recovery_cancelled = exc.args[:1] == ("fast_recovery",)
                    raise
                finally:
                    # Drains queue + joins worker + resets session.tts_* fields.
                    await voice.aclose(cancelled=turn_cancelled)

                # --- Success-path persistence + state updates ---
                no_reply = source == "user" and is_no_reply(result.full_response)
                trace_delivery = TRACE_SUPPRESSED if no_reply else delivery
                await self._persist_trace(
                    owner_id, source, result.turn_trace,
                    turn_id=_turn_id, delivery=trace_delivery, origin=origin,
                    skip_initial_user=user_row_persisted,
                    presence_metadata=presence_metadata,
                )
                if user_row_persisted:
                    await mongodb.mark_user_turn_status(
                        owner_id,
                        _turn_id,
                        "completed",
                        delivery=TRACE_SUPPRESSED if no_reply else None,
                    )
                if no_reply:
                    await self.manager.send_voice_response(
                        connection_id,
                        WSMessageType.NO_REPLY,
                        {"text": "Jarvis didn't reply."},
                        message_id=_turn_id,
                    )
                persisted = True
                perf.log(
                    "turn_persisted",
                    session=connection_id,
                    trace_entries=len(result.turn_trace),
                    tools_called=len(result.tools_called),
                    response_chars=len(result.full_response or result.delivered_text),
                    audio_sent=voice.first_audio_sent,
                    no_reply=no_reply,
                )
                # Voice turn completed successfully. The next utterance gets a new
                # VoiceInputTurn/transcript row; fast recovery preserves this object earlier.
                clear_voice_turn_if_current()

                logger.debug(f"LLM Response [{connection_id}]: {result.full_response}")

                # For turns without audio, update UI immediately — no playback_end will arrive.
                # For audio turns, handle_playback_end sends the correct state after audio finishes.
                # Mode may still be ACTIVE_AI_TURN here; finally owns the transition. Predict idle
                # when intentional silence closes the conversation window (NO_REPLY / soft mute).
                if not voice.first_audio_sent:
                    soft_muted = bool(getattr(session, "soft_muted", False))
                    final_stage = (
                        "idle"
                        if no_reply or soft_muted or session.processor.mode == VoiceMode.PASSIVE
                        else "listening"
                    )
                    await self.manager.send_voice_response(connection_id, WSMessageType.STATUS, {"stage": final_stage})

        except asyncio.CancelledError as exc:
            fast_recovery_cancelled = fast_recovery_cancelled or exc.args[:1] == ("fast_recovery",)
            perf.log(
                "process_turn_cancelled",
                session=connection_id,
                fast_recovery=fast_recovery_cancelled,
                trace_entries=len(result.turn_trace),
                delivered_chars=len(result.delivered_text),
                tools_called=len(result.tools_called),
            )
            # voice.aclose already ran in the inner finally; result.turn_trace
            # carries whatever was accumulated before the cancel.
            if not fast_recovery_cancelled and not persisted and result.turn_trace:
                await self._persist_trace(
                    owner_id, source, result.turn_trace,
                    turn_id=_turn_id, delivery=delivery, origin=origin,
                    skip_initial_user=user_row_persisted,
                    presence_metadata=presence_metadata,
                )
                if user_row_persisted:
                    await mongodb.mark_user_turn_status(owner_id, _turn_id, "cancelled")
            raise

        finally:
            # Mode ownership: ACTIVE_AI_TURN must not outlive this run unless playback is
            # still pending (defer to audio.playback_end). Matches interruption L224-225.
            # Text turns never touch set_mode, so skip entirely.
            if produce_audio:
                # Fast recovery is a cancellation of this processing attempt, not the
                # user voice turn. Preserve the turn id and latency timer for the merged
                # attempt that will merge the new continuation transcript.
                if fast_recovery_cancelled:
                    logger.debug("Turn finally: fast recovery hand-off, preserving turn_latency=%s", _turn_id)
                    perf.log("process_turn_finally", session=connection_id, outcome="fast_recovery_handoff")
                elif voice is None or not voice.first_audio_sent:
                    perf.end("response_latency", connection_id, status="no_audio_sent")
                    perf.end("turn_latency", connection_id, status="no_audio_sent")
                    clear_voice_turn_if_current()
                    soft_muted = bool(getattr(session, "soft_muted", False))
                    if soft_muted:
                        logger.debug("Turn finally: no audio sent, preserving soft-muted passive mode")
                        session.processor.force_passive(
                            reason="orchestrator.turn_finally_no_audio.soft_muted",
                        )
                    elif no_reply:
                        logger.debug("Turn finally: user NO_REPLY, closing to passive")
                        session.processor.force_passive(
                            reason="orchestrator.user_no_reply",
                            release_wake_refractory=True,
                            arm_post_tts_suppression=False,
                        )
                    else:
                        logger.debug("Turn finally: no audio sent, resetting mode directly")
                        session.processor.set_mode(VoiceMode.ACTIVE_IDLE, source="orchestrator.turn_finally_no_audio")
                        if hasattr(session.processor, "refresh_activity"):
                            session.processor.refresh_activity(source="orchestrator.turn_finally_no_audio")
                    perf.log(
                        "process_turn_finally",
                        session=connection_id,
                        outcome="user_no_reply" if no_reply and not soft_muted else "no_audio_sent",
                    )
                elif turn_cancelled or not getattr(session, "active_audio_turn_id", None):
                    # Cancelled after audio, or run ended with nothing awaiting playback.
                    clear_voice_turn_if_current()
                    if session.processor.mode == VoiceMode.ACTIVE_AI_TURN:
                        if getattr(session, "soft_muted", False):
                            session.processor.force_passive(reason="orchestrator.turn_finally_release.soft_muted")
                        else:
                            session.processor.set_mode(
                                VoiceMode.ACTIVE_IDLE,
                                source="orchestrator.turn_finally_release",
                            )
                            if hasattr(session.processor, "refresh_activity"):
                                session.processor.refresh_activity(source="orchestrator.turn_finally_release")
                    perf.log(
                        "process_turn_finally",
                        session=connection_id,
                        outcome="cancelled_release" if turn_cancelled else "no_pending_playback",
                    )
                else:
                    clear_voice_turn_if_current()
                    logger.debug("Turn finally: audio was sent, mode=%s (deferred to playback_end)", session.processor.mode.name)
                    perf.log("process_turn_finally", session=connection_id, outcome="audio_sent")
                    # Do NOT call refresh_activity here. The activity timer must be anchored
                    # to when audio playback actually ends (handle_playback_end), not when TTS
                    # generation finishes. Calling it here causes SESSION_ENDED to fire
                    # immediately after ACTIVE_IDLE starts for any response >8s of audio.

            # Clean up task reference only if it's the current task
            cleared_current_run_task = False
            if session.current_run_task == asyncio.current_task():
                session.current_run_task = None
                cleared_current_run_task = True
            if cleared_current_run_task and produce_audio and voice is not None:
                await voice.send_tts_end_if_ready()
            if not turn_lock_acquired:
                perf.discard("turn_lock_wait", connection_id)
            perf.reset_context(perf_token)
            reset_log_context(log_token)

        # Publish event
        await event_bus.publish(
            Event(
                type=EventType.VOICE_TURN_END,
                source="orchestrator",
                data={"session_id": connection_id, "transcript": transcript}
            )
        )

    async def _execute_turn(
        self,
        transcript: str,
        *,
        source: str,
        connection_id: str,
        owner_id: str,
        session_context: Dict[str, Any],
        text_input: bool,
        attachments: Optional[List[Dict[str, Any]]],
        delivery: Any,
        result: TurnResult,
        routing_hint: Optional[str] = None,
        current_turn_id: Optional[str] = None,
        history_policy: HistoryPolicy | None = None,
        trigger_decision: str | None = None,
    ) -> None:
        """Delegate to the delivery-agnostic execution module."""
        await execute_turn(
            agent=self.agent,
            transcript=transcript,
            source=source,
            connection_id=connection_id,
            owner_id=owner_id,
            session_context=session_context,
            text_input=text_input,
            attachments=attachments,
            delivery=delivery,
            result=result,
            routing_hint=routing_hint,
            current_turn_id=current_turn_id,
            history_policy=history_policy,
            trigger_decision=trigger_decision,
        )
