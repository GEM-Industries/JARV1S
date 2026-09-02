"""WebSocket message handlers with streaming STT."""

import logging
import asyncio
import contextlib
import wave
import io
import datetime
import time
from typing import Dict, Callable, Awaitable, Optional
import base64

from pydantic import ValidationError
from services.events import event_bus, Event, EventType
from services.perf import perf
from core.attention.models import AttentionMode, AttentionState
from core.attention.service import attention_service
from core.id import generate_id
from core.plugins.registry import registry
from core.voice.runtime import switchable_stt
from core.voice.streaming_stt import StreamingSTTCoordinator
from core.voice.turn_detector import AudioTurnDetectorSession, TurnDecision
from core.voice.runtime import switchable_tts
from core.voice.local_commands import (
    LocalVoiceCommand,
    normalize_local_command,
    resolve_local_command,
)
from core.voice.turn_admission import (
    AdmissionAction,
    AdmissionDecision,
    BargeInEvidence,
    Directedness,
    FollowupEvidence,
    decide_barge_in_admission,
    decide_followup_admission,
)
from core.voice.speaker_verifier import (
    MIN_SCORE_SECONDS,
    SpeakerEvidence,
    SpeakerMatchStatus,
)
from core.turns.delivery import signal_current_delivery_cancel
from core.turns.orchestrator import AssistantOrchestrator, SESSION_FRESHNESS_SECONDS
from core.voice.processor import SpeechEvent, SpeechTurnPhase, VoiceMode
from core.voice import turn_input as voice_turn_input
from core.llm.service import LLMService
from core.agent.agent import JarvisAgent
from core.config import settings
from core.voice.config import resolve_voice_config_sync
from core.setup.readiness import SetupNotReadyError, require_llm_ready
from .models import ClientDiagnosticBatch, WSMessage, WSResponse
from .types import WSMessageType
from .connection import Session, VoiceInputTurn, attention_state_payload, session_state_payload, manager
from core.plugins.pinned_widgets import pin_widget, unpin_widget
from core.plugins.types import UIEnvelope
from core.plugins.ui_handler import process_ui_action
from plugins.attention import apply_soft_mute_for_session, clear_soft_mute_for_session

logger = logging.getLogger(__name__)

_LOCAL_UNMUTE_ACK = "Online."
_LOCAL_POWER_CHECK_ACK = "For you sir, always."


async def _resolve_pending_confirmation(session_id: str, session, text: str) -> bool:
    from core.plugins.consent import resolve_pending_from_utterance

    owner_id = getattr(session, "owner_id", session_id)
    result = await resolve_pending_from_utterance(owner_id, text)
    if result is None:
        return False
    session.voice_turn = None
    await orchestrator._deliver_text(
        session_id,
        result,
        None,
        delivery="local_command",
        persist=False,
    )
    return True

# Per-owner/node client diagnostic budget (events / rolling window).
_CLIENT_DIAG_BUDGET = 60
_CLIENT_DIAG_WINDOW_S = 60.0
_client_diag_budget: dict[str, tuple[float, int]] = {}
_client_diag_last_warning: dict[str, tuple[str, float]] = {}


async def _set_attention_mode_fast_path(
    owner_id: str,
    node_id: str | None,
    mode: AttentionMode,
) -> AttentionState:
    """Use the attention plugin semantic entrypoint; fall back only if registry is not ready."""
    plugin = registry.plugins.get("attention")
    setter = getattr(plugin, "set_mode_for_identity", None) if plugin else None
    if setter:
        return await setter(owner_id, node_id, mode, source="local_command")

    return await attention_service.set_mode(owner_id, mode, source="local_command")


async def _get_attention_mode_fast_path(owner_id: str) -> AttentionMode:
    """Use the attention plugin semantic entrypoint; fall back only if registry is not ready."""
    plugin = registry.plugins.get("attention")
    getter = getattr(plugin, "get_mode_for_identity", None) if plugin else None
    if getter:
        return await getter(owner_id)

    return await attention_service.get_mode(owner_id)


async def _dismiss_active_trigger_delivery(session) -> None:
    """Explicit STOP means dismiss the active trigger, not retry delivery later."""
    instance_id = getattr(session, "current_trigger_instance_id", None)
    if not instance_id:
        return

    from core.triggers.service import trigger_service

    if not await trigger_service.acknowledge_instance(instance_id):
        await trigger_service.cancel_instance(instance_id)


def _ensure_voice_turn(session_id: str, session) -> VoiceInputTurn:
    """Start user-facing voice latency at detected speech, before endpoint/STT finalize."""
    if session.voice_turn is not None:
        return session.voice_turn

    return _start_new_voice_turn(session_id, session)


def _create_voice_turn(session_id: str) -> VoiceInputTurn:
    turn_id = generate_id("turn-")
    voice_config = resolve_voice_config_sync()
    voice_turn = VoiceInputTurn(turn_id=turn_id)
    perf.start("turn_latency", session_id, turn_id=turn_id, source="user", scenario="voice")
    perf.log(
        "voice_turn_started",
        session=session_id,
        turn_id=turn_id,
        source="user",
        scenario="voice",
        stt_backend=voice_config.stt_provider,
        stt_streaming_enabled=settings.VOICE.stt_streaming_enabled,
        silence_threshold_s=settings.VOICE.silence_threshold,
    )
    return voice_turn


def _start_new_voice_turn(session_id: str, session) -> VoiceInputTurn:
    """Create a fresh logical user utterance. Late continuation is the only reuse path."""
    session.voice_turn = None

    session.voice_turn = _create_voice_turn(session_id)
    return session.voice_turn


async def _publish_voice_user_start(session_id: str, session) -> None:
    """Publish a user-speech interruption only when the speech is actionable."""
    if getattr(session, "soft_muted", False):
        logger.debug("Suppressing VOICE_USER_START for %s: session is soft_muted", session_id)
        return
    await event_bus.publish(
        Event(
            type=EventType.VOICE_USER_START,
            source="websocket",
            data={"session_id": session_id},
        )
    )


def _discard_voice_turn_latency(session_id: str, session, *, reason: str = "unspecified") -> None:
    voice_turn = session.voice_turn
    session.voice_turn = None
    _discard_turn_latency(session_id, voice_turn, reason=reason)


def _wake_turn_has_request(text: str) -> bool:
    """True when transcript has content beyond wake/fillers (shared local-command normalize)."""
    return bool(normalize_local_command(text).strip())


async def _settle_wake_followon(
    session_id: str,
    session,
    voice_turn: VoiceInputTurn,
    *,
    reason: str,
) -> None:
    """Drop a wake-only turn without LLM; keep ACTIVE_IDLE so follow-on speech needs no re-wake."""
    await _close_streaming_stt(session, reason=reason)
    await _close_turn_detector(session)
    session.processor.force_active(reason=reason)
    _discard_turn_latency(session_id, voice_turn, reason=reason)
    if session.voice_turn is voice_turn:
        session.voice_turn = None
    perf.log(
        "wake_followon_settled",
        session=session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        reason=reason,
    )
    await manager.send_voice_response(
        session_id,
        WSMessageType.STATUS,
        {"stage": "listening"},
    )


def _discard_turn_latency(
    session_id: str,
    voice_turn: VoiceInputTurn | None,
    *,
    reason: str = "unspecified",
) -> None:
    if voice_turn:
        perf.log(
            "voice_turn_discarded",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
            reason=reason,
        )
        perf.discard("turn_latency", session_id, turn_id=voice_turn.turn_id, source="user", scenario="voice")


def _pcm_duration_ms(pcm_bytes_or_len: bytes | bytearray | int) -> float:
    return voice_turn_input.pcm_duration_ms(pcm_bytes_or_len)


def _eou_visibility_fields(voice_turn: VoiceInputTurn, *, now: float | None = None) -> dict[str, float | int]:
    """LiveKit-aligned speech-end→commit visibility for turn_runs.turn_detection."""
    now = time.monotonic() if now is None else now
    fields: dict[str, float | int] = {
        "continue_count": voice_turn.continue_count,
        "awaiting_stt_count": voice_turn.awaiting_stt_count,
        "vad_endpoint_count": voice_turn.vad_endpoint_count,
    }
    if voice_turn.speech_ended_at > 0:
        fields["end_of_turn_delay_ms"] = round((now - voice_turn.speech_ended_at) * 1000, 1)
        if voice_turn.first_transcript_at > 0:
            fields["transcription_delay_ms"] = round(
                max(0.0, voice_turn.first_transcript_at - voice_turn.speech_ended_at) * 1000,
                1,
            )
    return fields


def _endpointing_snapshot() -> dict[str, str]:
    """Fingerprint of endpointing knobs for A/B scorecards on turn_runs."""
    voice = settings.VOICE
    return {
        "endpointing_profile": (
            f"v3,vad={voice.silence_threshold:.2f},"
            f"min={voice.turn_detector_min_delay:.2f},"
            f"amax={voice.apple_speech_endpoint_max_delay:.2f},"
            f"stable={voice.apple_speech_commit_stability_delay:.2f},"
            f"sttw={voice.turn_detector_awaiting_stt_timeout:.2f}"
        ),
    }


def _stt_coverage_fields(turn_audio_bytes: int, bytes_fed: int) -> dict[str, float | int | None]:
    """Compare captured turn PCM to bytes actually fed into the streaming STT session."""
    return voice_turn_input.stt_coverage_fields(turn_audio_bytes, bytes_fed)


def _text_tail(text: str, limit: int = 120) -> str:
    return voice_turn_input.text_tail(text, limit)


def _voice_trace(session_id: str, event: str, **fields) -> None:
    """Stdout trace for fast-recovery / batch-STT debugging. Enable via VOICE__trace_voice_events."""
    if not settings.VOICE.trace_voice_events:
        return
    parts = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    logger.info("Voice trace | session=%s event=%s %s", session_id, event, parts)


def _merge_continuation_text(base: str, update: str) -> str:
    """Join prior accepted text with a continuation stream, avoiding simple word overlap."""
    return voice_turn_input.merge_continuation_text(base, update)


def _normalise_for_overlap(text: str) -> str:
    return voice_turn_input.normalise_for_overlap(text)


def _overlap_words(text: str) -> list[str]:
    return voice_turn_input.overlap_words(text)


def _apply_voice_turn_transcript(
    session_id: str,
    voice_turn: VoiceInputTurn,
    candidate: str,
    *,
    event: str,
    reason: str,
    stream_id: str | None = None,
) -> bool:
    """Accept transcript candidates monotonically so provider revisions cannot erase better text."""
    candidate = candidate.strip()
    if not candidate:
        return False

    prior_text = voice_turn.transcript_text
    if candidate == prior_text:
        return True

    prior_chars = len(prior_text)
    candidate_chars = len(candidate)
    regressed = candidate_chars < prior_chars
    if settings.VOICE.trace_voice_events or regressed:
        perf.log(
            event,
            session=session_id,
            turn_id=voice_turn.turn_id,
            stream_id=stream_id,
            source="user",
            scenario="voice",
            reason=reason,
            prior_chars=prior_chars,
            candidate_chars=candidate_chars,
            delta_chars=candidate_chars - prior_chars,
            accepted=not regressed,
            regressed=regressed,
            prior_tail=_text_tail(prior_text) if regressed else None,
            candidate_tail=_text_tail(candidate) if regressed else None,
        )

    if regressed:
        _voice_trace(
            session_id,
            "transcript_rejected",
            turn_id=voice_turn.turn_id,
            reason=reason,
            prior_tail=_text_tail(prior_text, 80),
            candidate_tail=_text_tail(candidate, 80),
        )
        return False

    if not prior_text and voice_turn.first_transcript_at <= 0:
        voice_turn.first_transcript_at = time.monotonic()
    voice_turn.transcript_text = candidate
    return True


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1, sample_width: int = 2) -> bytes:
    """Wrap raw 16-bit PCM bytes in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


# --- Core Components ---
stt = switchable_stt
llm = LLMService()
agent = JarvisAgent(llm_service=llm)
tts = switchable_tts
orchestrator = AssistantOrchestrator(stt=stt, llm=llm, agent=agent, tts=tts)


async def initialize_llm_component():
    """Initialize only the LLM client needed for text turns."""
    try:
        await llm.initialize()
        logger.info("LLM component initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize LLM component: %s", e)


# --- Message Handler Registry ---
MessageHandler = Callable[[str, WSMessage], Awaitable[None]]
message_handlers: Dict[WSMessageType, MessageHandler] = {}


def register_handler(message_type: WSMessageType) -> Callable[[MessageHandler], MessageHandler]:
    """Decorator to register message handlers."""
    def decorator(handler: MessageHandler) -> MessageHandler:
        message_handlers[message_type] = handler
        return handler
    return decorator


async def handle_message(session_id: str, message: WSMessage) -> None:
    """Route messages to appropriate handlers."""
    try:
        session = manager.get_session(session_id)
        if session:
            was_stale = not session.is_fresh(max_age_s=SESSION_FRESHNESS_SECONDS)
            session.touch()
            if was_stale:
                logger.info(
                    "Session %s recovered from stale websocket. Checking awaiting trigger delivery.",
                    session_id,
                )
                await event_bus.publish(
                    Event(
                        type=EventType.SESSION_CONNECTED,
                        source="websocket_freshness_recovered",
                        data={
                            "owner_id": session.owner_id,
                            "session_id": session.owner_id,
                            "connection_id": session.connection_id,
                            "node_id": session.presence.node_id,
                        },
                    )
                )
        if message.type in message_handlers:
            await message_handlers[message.type](session_id, message)
        else:
            logger.warning(f"No handler for message type: {message.type}")
            await manager.send_message(
                session_id,
                WSResponse(
                    message_id=message.id,
                    type=WSMessageType.ERROR,
                    error=f"Unsupported message type: {message.type}"
                )
            )
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=message.id,
                type=WSMessageType.ERROR,
                error=str(e)
            )
        )


# --- System Handlers ---
@register_handler(WSMessageType.PING)
async def handle_ping(session_id: str, message: WSMessage) -> None:
    """Handle ping messages."""
    from services.diagnostics import diagnostics_service
    await manager.send_message(
        session_id,
        WSResponse(
            message_id=message.id,
            type=WSMessageType.PONG,
            data={
                "core_id": settings.SYSTEM_NAME,
                "diagnostics": diagnostics_service.snapshot,
                "presence": manager.get_presence_snapshot(),
            },
        )
    )


@register_handler(WSMessageType.EVENT_SUBSCRIBE)
async def handle_event_subscribe(session_id: str, message: WSMessage) -> None:
    """Handle event subscription requests."""
    event_type = message.data.get("event_type")
    if event_type:
        await manager.handle_subscription(session_id, event_type)
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=message.id,
                type=WSMessageType.EVENT_SUBSCRIBE,
                data={"status": "subscribed", "event_type": event_type}
            )
        )


@register_handler(WSMessageType.EVENT_UNSUBSCRIBE)
async def handle_event_unsubscribe(session_id: str, message: WSMessage) -> None:
    """Handle event unsubscription requests."""
    event_type = message.data.get("event_type")
    if event_type:
        await manager.handle_unsubscription(session_id, event_type)
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=message.id,
                type=WSMessageType.EVENT_UNSUBSCRIBE,
                data={"status": "unsubscribed", "event_type": event_type}
            )
        )


@register_handler(WSMessageType.CONTEXT_UPDATE)
async def handle_context_update(session_id: str, message: WSMessage) -> None:
    """Handle client context updates (e.g., location)."""
    from core.context import parse_geo_position

    session = manager.get_session(session_id)
    if not session:
        return

    if isinstance(message.data, dict):
        allowed_updates: dict[str, object] = {}
        if "timezone" in message.data and isinstance(message.data["timezone"], str):
            timezone = message.data["timezone"].strip()
            if timezone:
                allowed_updates["timezone"] = timezone
        if "location" in message.data:
            location_payload = message.data["location"]
            if isinstance(location_payload, dict) and not location_payload.get("captured_at"):
                location_payload = {
                    **location_payload,
                    "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
            try:
                geo = parse_geo_position(location_payload)
            except ValueError as exc:
                logger.warning("Rejected invalid location update for %s: %s", session_id, exc)
                await manager.send_message(
                    session_id,
                    WSResponse(
                        message_id=message.id,
                        type=WSMessageType.STATUS,
                        data={"status": "context_update_rejected", "error": "invalid_location"},
                    ),
                )
                return
            if geo is None:
                allowed_updates["location"] = None
            else:
                allowed_updates["location"] = geo.as_dict()
        if allowed_updates:
            session.context.update(allowed_updates)
            logger.debug(
                "Updated context for session %s: %s",
                session_id,
                list(allowed_updates.keys()),
            )

    await manager.send_message(
        session_id,
        WSResponse(
            message_id=message.id,
            type=WSMessageType.STATUS,
            data={"status": "context_updated"}
        )
    )


def _client_diag_budget_key(session: Session) -> str:
    """Stable across reconnects so flapping clients cannot reset the budget."""
    return f"{session.owner_id}:{session.presence.node_id}"


def _client_diag_prune(now: float) -> None:
    stale_budgets = [
        key
        for key, (window_start, _) in _client_diag_budget.items()
        if now - window_start >= _CLIENT_DIAG_WINDOW_S * 2
    ]
    for key in stale_budgets:
        _client_diag_budget.pop(key, None)

    stale_warnings = [
        key
        for key, (_, logged_at) in _client_diag_last_warning.items()
        if now - logged_at >= _CLIENT_DIAG_WINDOW_S * 2
    ]
    for key in stale_warnings:
        _client_diag_last_warning.pop(key, None)


def _client_diag_allow(budget_key: str, count: int) -> int:
    """Return how many events from this batch may be accepted under the budget."""
    now = time.monotonic()
    window_start, used = _client_diag_budget.get(budget_key, (now, 0))
    if now - window_start >= _CLIENT_DIAG_WINDOW_S:
        window_start, used = now, 0
    remaining = max(0, _CLIENT_DIAG_BUDGET - used)
    accepted = min(remaining, count)
    _client_diag_budget[budget_key] = (window_start, used + accepted)
    return accepted


def _client_diag_dedupe_warning(budget_key: str, key: str) -> bool:
    """Return True if this warning should be logged (not a recent duplicate)."""
    now = time.monotonic()
    previous = _client_diag_last_warning.get(budget_key)
    if previous and previous[0] == key and now - previous[1] < 5.0:
        return False
    _client_diag_last_warning[budget_key] = (key, now)
    return True


@register_handler(WSMessageType.CLIENT_DIAGNOSTICS)
async def handle_client_diagnostics(session_id: str, message: WSMessage) -> None:
    """Accept bounded client incident breadcrumbs; log with trusted identity."""
    session = manager.get_session(session_id)
    if not session:
        return

    _client_diag_prune(time.monotonic())
    budget_key = _client_diag_budget_key(session)
    try:
        batch = ClientDiagnosticBatch.model_validate(message.data or {})
    except ValidationError:
        if _client_diag_dedupe_warning(budget_key, "invalid"):
            logger.warning(
                "Client diagnostics rejected | connection=%s node=%s",
                session.connection_id,
                session.presence.node_id,
            )
        return

    accepted = _client_diag_allow(budget_key, len(batch.events))
    if accepted <= 0:
        if _client_diag_dedupe_warning(budget_key, "budget"):
            logger.warning(
                "Client diagnostics rate-limited | connection=%s node=%s kind=%s",
                session.connection_id,
                session.presence.node_id,
                session.presence.device_kind,
            )
        return

    events = batch.events[:accepted]
    if accepted < len(batch.events) and _client_diag_dedupe_warning(budget_key, "budget_partial"):
        logger.warning(
            "Client diagnostics partially accepted | connection=%s node=%s accepted=%d dropped=%d",
            session.connection_id,
            session.presence.node_id,
            accepted,
            len(batch.events) - accepted,
        )

    received_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if batch.dropped_count:
        logger.info(
            "ClientDiag batch connection=%s node=%s kind=%s client_dropped=%d accepted=%d",
            session.connection_id,
            session.presence.node_id,
            session.presence.device_kind,
            batch.dropped_count,
            len(events),
        )
    for event in events:
        meta_bits = " ".join(
            f"{key}={value!r}" for key, value in sorted(event.metadata.items())
        )
        logger.info(
            "ClientDiag event=%s severity=%s connection=%s node=%s kind=%s "
            "turn_id=%s message_id=%s seq=%d client_ts=%s received_at=%s %s",
            event.event,
            event.severity,
            session.connection_id,
            session.presence.node_id,
            session.presence.device_kind,
            event.turn_id,
            event.message_id,
            event.seq,
            event.ts.isoformat(),
            received_at,
            meta_bits,
        )


@register_handler(WSMessageType.PLAYBACK_END)
async def handle_playback_end(session_id: str, message: WSMessage) -> None:
    """Handle notification that frontend finished playing audio."""
    session = manager.get_session(session_id)
    if not session:
        return

    payload_turn_id = message.data.get("turn_id") if isinstance(message.data, dict) else None
    playback_turn_id = payload_turn_id if isinstance(payload_turn_id, str) and payload_turn_id else None
    active_audio_turn_id = getattr(session, "active_audio_turn_id", None)
    stale_playback_end = bool(
        playback_turn_id is not None
        and active_audio_turn_id is not None
        and playback_turn_id != active_audio_turn_id
    )
    turn_active = bool(session.current_run_task and not session.current_run_task.done())
    logger.info(
        "Playback ended | session=%s turn_active=%s turn_id=%s active_audio_turn_id=%s stale=%s",
        session_id,
        turn_active,
        playback_turn_id,
        active_audio_turn_id,
        stale_playback_end,
    )
    perf.log(
        "playback_end_received",
        session=session_id,
        owner_id=session.owner_id,
        connection_id=session.connection_id,
        node_id=session.presence.node_id,
        turn_active=turn_active,
        turn_id=playback_turn_id or active_audio_turn_id,
        active_audio_turn_id=active_audio_turn_id,
        stale=stale_playback_end,
        mode=session.processor.mode.name,
        first_audio_sent=session.first_audio_sent,
        last_turn_audio_sent=session.last_turn_audio_sent,
    )

    if stale_playback_end:
        if turn_active:
            session.processor.refresh_activity(source="playback_end_stale")
    elif turn_active:
        # Mid-turn audio gap (e.g. between pre-tool sentence and post-tool response).
        # Keep mode as ACTIVE_AI_TURN so echo suppression and barge-in threshold stay armed.
        # Just refresh the activity timer so this silent gap doesn't count against the 30s.
        session.processor.refresh_activity(source="playback_end_mid_turn")
    elif session.processor.mode == VoiceMode.ACTIVE_AI_TURN:
        # Turn is complete — arm echo cooldown and return to idle listening.
        # Reset the activity timer HERE (anchored to actual playback end, not TTS generation end)
        # so the 8-second listening window starts from when audio actually stops playing.
        if getattr(session, "soft_muted", False):
            session.processor.force_passive(reason="ws.audio_playback_end.soft_muted")
        else:
            session.processor.set_mode(VoiceMode.ACTIVE_IDLE, source="ws.audio_playback_end")
            session.processor.refresh_activity(source="playback_end")
        if playback_turn_id and playback_turn_id == active_audio_turn_id:
            session.active_audio_turn_id = None
    # If already PASSIVE (e.g. mute fired just before playback_end), leave it alone.

    # Send the correct post-speech state: listening (ACTIVE window) or idle (PASSIVE, wake word needed)
    stage = "listening" if session.processor.mode != VoiceMode.PASSIVE else "idle"
    await manager.send_message(
        session_id,
        WSResponse(
            message_id=message.id,
            type=WSMessageType.STATUS,
            data={"stage": stage}
        )
    )


@register_handler(WSMessageType.MUTE)
async def handle_mute(session_id: str, message: WSMessage) -> None:
    """Handle mic mute — reset processor to PASSIVE without interrupting active playback."""
    session = manager.get_session(session_id)
    if not session:
        return

    _clear_barge_in_candidate(session, reason="mute")
    _cancel_followup_identity_task(session, reason="mute")
    _cancel_endpoint_decision(session, reason="mute")
    await _close_streaming_stt(session, reason="mute")
    _discard_voice_turn_latency(session_id, session, reason="mute")
    session.processor.force_passive(reason="ws.audio_mute")
    perf.log(
        "voice_mute",
        session=session_id,
        mode=session.processor.mode.name,
        turn_active=bool(session.current_run_task and not session.current_run_task.done()),
    )
    await manager.send_message(
        session_id,
        WSResponse(
            message_id=message.id,
            type=WSMessageType.STATUS,
            data={"stage": "idle"}
        )
    )


@register_handler(WSMessageType.VOICE_ACTIVATE)
async def handle_voice_activate(session_id: str, message: WSMessage) -> None:
    """Open the active listening window for push-to-talk and benchmark clients."""
    session = manager.get_session(session_id)
    if not session:
        return

    _clear_barge_in_candidate(session, reason="voice_activate")
    _cancel_followup_identity_task(session, reason="voice_activate")
    session.processor.force_active(reason="ws.voice_activate")
    await manager.send_message(
        session_id,
        WSResponse(
            message_id=message.id,
            type=WSMessageType.STATUS,
            data={"stage": "listening"},
        ),
    )


@register_handler(WSMessageType.VOICE_COMMIT)
async def handle_voice_commit(session_id: str, message: WSMessage) -> None:
    """Commit audio when an explicit push-to-talk gesture ends."""
    session = manager.get_session(session_id)
    if not session:
        return

    if not session.processor.request_turn_commit():
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=message.id,
                type=WSMessageType.STATUS,
                data={"stage": "listening"},
            ),
        )
        return

    _cancel_endpoint_decision(session, reason="push_to_talk_commit")
    voice_turn = _ensure_voice_turn(session_id, session)
    _sync_voice_turn_transcript(
        session_id,
        session,
        reason="push_to_talk_commit",
        voice_turn=voice_turn,
    )

    if _barge_in_candidate_active(session):
        decision = await _resolve_barge_in_candidate(
            session_id,
            session,
            endpointed=True,
            handoff_on_commit=False,
        )
        if decision is not AdmissionAction.COMMIT:
            return

    if voice_turn.admission_source is None:
        voice_turn.admission_source = "push_to_talk"
        voice_turn.admission_reason = "push_to_talk_release"

    await event_bus.publish(
        Event(
            type=EventType.VOICE_USER_END,
            source="websocket",
            data={"session_id": session_id},
        )
    )
    await manager.send_message(
        session_id,
        WSResponse(
            message_id=message.id,
            type=WSMessageType.STATUS,
            data={"stage": "transcribing"},
        ),
    )

    task = asyncio.current_task()
    session.accepted_input_task = task
    try:
        await _commit_voice_turn(
            session_id,
            session,
            voice_turn,
            TurnDecision(done=True, reason="push_to_talk_release"),
        )
    finally:
        if session.accepted_input_task is task:
            session.accepted_input_task = None


@register_handler(WSMessageType.STOP)
async def handle_stop_signal(session_id: str, message: WSMessage) -> None:
    """Handle explicit stop signal from client."""
    session = manager.get_session(session_id)
    if not session:
        return
        
    _clear_barge_in_candidate(session, reason="stop")
    _cancel_followup_identity_task(session, reason="stop")
    _cancel_endpoint_decision(session, reason="stop")
    await _close_streaming_stt(session, reason="stop")
    _discard_voice_turn_latency(session_id, session, reason="stop")
    perf.log(
        "voice_stop",
        session=session_id,
        mode=session.processor.mode.name,
        turn_active=bool(session.current_run_task and not session.current_run_task.done()),
    )
    await _dismiss_active_trigger_delivery(session)
    # 1. Publish interruption event to let Orchestrator handle cleanup (Cancel LLM/TTS)
    await event_bus.publish(
        Event(
            type=EventType.VOICE_INTERRUPT,
            source="websocket_stop_signal",
            data={"session_id": session_id}
        )
    )
    
    # 2. Force processor back to PASSIVE (Wait for wake-word)
    # This aligns the backend state with the "Stop" intent
    session.processor.force_passive(reason="ws.system_stop")
        
    # 3. Tell frontend to go idle
    await manager.send_message(
        session_id,
        WSResponse(
            message_id=message.id,
            type=WSMessageType.STATUS,
            data={"stage": "idle"}
        )
    )


@register_handler(WSMessageType.UI_ACTION)
async def handle_ui_action(session_id: str, message: WSMessage) -> None:
    """
    Handle user interaction with a widget.
    Calls the core UI handler and pushes any resulting updates back to the client.
    """
    plugin_name = message.data.get("plugin")
    tool_name = message.data.get("tool")
    args = message.data.get("args", {})

    if not plugin_name or not tool_name:
        logger.warning(f"Malformed UI action from {session_id}: {message.data}")
        return

    session = manager.get_session(session_id)
    if not session:
        return

    from core.context import RuntimeIdentity, ToolRuntimeContext, bind_tool_context, reset_tool_context
    token = bind_tool_context(
        ToolRuntimeContext(
            identity=RuntimeIdentity(
                owner_id=session.owner_id,
                connection_id=session.connection_id,
                node_id=session.presence.node_id,
                location_ref=session.presence.location.model_dump(),
                device_kind=session.presence.device_kind,
            ),
            timezone=session.context.get("timezone", "UTC"),
            location=session.context.get("location"),
            extras={
                "invocation_source": "ui_action",
            },
        )
    )
    try:
        # 1. Process action in Core
        result, ui_update = await process_ui_action(plugin_name, tool_name, args)
        
        # 2. Push UI Update if requested
        if ui_update:
            await manager.send_message(
                session_id,
                WSResponse(
                    message_id=message.id,
                    type=WSMessageType.UI_UPDATE,
                    data=ui_update.dict()
                )
            )
            
        # 3. Return status result
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=message.id,
                type=WSMessageType.STATUS,
                data={"result": str(result)}
            )
        )

    except Exception as e:
        logger.error(f"UI action failed for {plugin_name}.{tool_name}: {e}")
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=message.id,
                type=WSMessageType.ERROR,
                error=f"Action failed: {str(e)}"
            )
        )
    finally:
        reset_tool_context(token)


@register_handler(WSMessageType.UI_PIN)
async def handle_ui_pin(session_id: str, message: WSMessage) -> None:
    """Persist or clear a pinned widget for reconnect snapshots."""
    session = manager.get_session(session_id)
    if not session:
        return

    pinned = bool(message.data.get("pinned"))
    widget_id = message.data.get("widget_id")

    try:
        if pinned:
            envelope = UIEnvelope.model_validate(message.data.get("widget"))
            pinned_envelope = await pin_widget(session.owner_id, envelope)
            await manager.send_message(
                session_id,
                WSResponse(
                    message_id=message.id,
                    type=WSMessageType.UI_UPDATE,
                    data=pinned_envelope.model_dump(mode="json"),
                )
            )
        elif isinstance(widget_id, str):
            await unpin_widget(session.owner_id, widget_id)
        else:
            raise ValueError("widget_id is required when unpinning")

        await manager.send_message(
            session_id,
            WSResponse(
                message_id=message.id,
                type=WSMessageType.STATUS,
                data={"result": "ok"},
            )
        )
    except Exception as e:
        logger.error(f"UI pin failed for {session_id}: {e}")
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=message.id,
                type=WSMessageType.ERROR,
                error=f"Pin failed: {str(e)}",
            )
        )


# --- Text Input Handler ---
MAX_TEXT_INPUT_LENGTH = 10_000


def _drain_attachments(session) -> list | None:
    """Consume and return pending attachments from the session, or None if empty."""
    if not session.pending_attachments:
        return None
    attachments = list(session.pending_attachments)
    session.pending_attachments.clear()
    logger.debug(f"Drained {len(attachments)} attachment(s) for turn")
    return attachments


async def _send_partial_transcript(session_id: str, voice_turn, text: str) -> None:
    """Render interim user text in place (blue/partial), keyed to the voice turn id."""
    message_id = voice_turn.turn_id if voice_turn else None
    data: dict[str, str] = {"text": text}
    if voice_turn is not None:
        data["turn_id"] = voice_turn.turn_id
    await manager.send_voice_response(
        session_id,
        WSMessageType.PARTIAL_TRANSCRIPT,
        data,
        message_id=message_id,
    )


async def _start_streaming_stt(
    session_id: str,
    session,
    initial_audio: bytes,
    *,
    voice_turn: VoiceInputTurn | None = None,
) -> None:
    """Start per-turn streaming STT and seed it with already-buffered speech."""
    if session.stt_stream is not None:
        _feed_turn_detector(session, initial_audio)
        return
    voice_turn = voice_turn or session.voice_turn

    from core.voice.service import get_voice_input_status

    # Prefer cached readiness on the turn hot path; avoid a status WebSocket every turn.
    input_status = await get_voice_input_status()
    if not input_status.ready:
        detail = input_status.detail or "Voice input is not ready."
        perf.log(
            "stt_stream_not_ready",
            session=session_id,
            turn_id=voice_turn.turn_id if voice_turn else None,
            source="user",
            scenario="voice",
            provider=input_status.provider,
            state=input_status.state,
        )
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=generate_id(),
                type=WSMessageType.ERROR,
                error=detail,
                data={"code": "voice_input_not_ready", "state": input_status.state},
            ),
        )
        return

    async def send_partial(text: str) -> None:
        if voice_turn is not None:
            candidate = _merge_continuation_text(voice_turn.continuation_prefix, text)
            prior_text = voice_turn.transcript_text
            accepted = _apply_voice_turn_transcript(
                session_id,
                voice_turn,
                candidate,
                event="stt_partial_transcript_applied",
                reason="stream_partial",
                stream_id=getattr(session.stt_stream, "stream_id", None),
            )
            if not accepted or voice_turn.transcript_text == prior_text:
                return
            text = voice_turn.transcript_text

        await _send_partial_transcript(session_id, voice_turn, text)

    async def provider_turn_end(text: str) -> None:
        if voice_turn is None:
            return
        if text:
            candidate = _merge_continuation_text(voice_turn.continuation_prefix, text)
            _apply_voice_turn_transcript(
                session_id,
                voice_turn,
                candidate,
                event="stt_provider_turn_end_applied",
                reason="provider_turn_end",
                stream_id=getattr(session.stt_stream, "stream_id", None),
            )
        if not getattr(getattr(stt, "capabilities", None), "provider_turn_events", False):
            return
        if session.accepted_input_task is not None and not session.accepted_input_task.done():
            return
        if voice_turn is getattr(session, "barge_in_candidate_turn", None):
            asyncio.create_task(_resolve_barge_in_candidate(session_id, session, endpointed=True))
            return
        asyncio.create_task(_commit_provider_turn_end(session_id, session, voice_turn))

    coordinator = StreamingSTTCoordinator(
        stt=stt,
        session_id=session_id,
        turn_id=voice_turn.turn_id if voice_turn else None,
        on_partial=send_partial,
        on_provider_turn_end=provider_turn_end,
    )
    perf.log(
        "stt_stream_start_requested",
        session=session_id,
        turn_id=voice_turn.turn_id if voice_turn else None,
        existing_transcript_chars=len(voice_turn.transcript_text) if voice_turn else 0,
        stream_id=coordinator.stream_id,
        source="user",
        scenario="voice",
        initial_audio_ms=_pcm_duration_ms(initial_audio),
        initial_audio_bytes=len(initial_audio),
    )
    started = await coordinator.start(initial_audio)
    _feed_turn_detector(session, initial_audio)
    if not started:
        voice_config = resolve_voice_config_sync()
        perf.log(
            "stt_stream_unavailable",
            session=session_id,
            turn_id=voice_turn.turn_id if voice_turn else None,
            stream_id=coordinator.stream_id,
            source="user",
            scenario="voice",
            backend=voice_config.stt_provider,
        )
        return
    session.stt_stream = coordinator
    perf.log(
        "stt_stream_active",
        session=session_id,
        turn_id=voice_turn.turn_id if voice_turn else None,
        stream_id=coordinator.stream_id,
        source="user",
        scenario="voice",
    )


async def _feed_streaming_stt(session, audio_bytes: bytes) -> None:
    _feed_turn_detector(session, audio_bytes)
    if session.stt_stream is None:
        return
    await session.stt_stream.feed(audio_bytes)


def _ensure_turn_detector(session) -> AudioTurnDetectorSession:
    detector = getattr(session, "turn_detector", None)
    if detector is None:
        detector = AudioTurnDetectorSession(
            sample_rate=settings.VOICE.sample_rate,
            channels=settings.VOICE.channels,
        )
        session.turn_detector = detector
    return detector


def _feed_turn_detector(session, audio_bytes: bytes) -> None:
    if not audio_bytes:
        return
    _ensure_turn_detector(session).push_pcm(audio_bytes)


def _flush_turn_detector(session) -> None:
    detector = getattr(session, "turn_detector", None)
    if detector is not None:
        detector.flush()


async def _close_turn_detector(session) -> None:
    detector = getattr(session, "turn_detector", None)
    session.turn_detector = None
    if detector is not None:
        await detector.aclose()


def _sync_voice_turn_transcript(
    session_id: str,
    session,
    *,
    reason: str,
    voice_turn: VoiceInputTurn | None = None,
) -> str:
    voice_turn = voice_turn or session.voice_turn
    if voice_turn is None:
        return ""

    stream_text = session.stt_stream.latest_text if session.stt_stream is not None else ""
    if stream_text:
        candidate = _merge_continuation_text(voice_turn.continuation_prefix, stream_text)
        _apply_voice_turn_transcript(
            session_id,
            voice_turn,
            candidate,
            event="stt_transcript_sync",
            reason=reason,
            stream_id=getattr(session.stt_stream, "stream_id", None),
        )
    return voice_turn.transcript_text.strip()


def _apple_speech_transcript_stability(session, now: float) -> tuple[bool, float | None]:
    if resolve_voice_config_sync().stt_provider != "apple_speech" or session.stt_stream is None:
        return True, None
    if getattr(session.stt_stream, "latest_text_is_final", False):
        return True, None
    delay = settings.VOICE.apple_speech_commit_stability_delay
    if delay <= 0:
        return True, None
    updated_at = getattr(session.stt_stream, "latest_text_updated_at", 0.0)
    if updated_at <= 0:
        return True, None
    age = now - updated_at
    return age >= delay, age


def _apple_speech_endpoint_wait_remaining(session, voice_turn: VoiceInputTurn) -> float | None:
    if resolve_voice_config_sync().stt_provider != "apple_speech" or session.stt_stream is None:
        return None
    if getattr(session.stt_stream, "latest_text_updated_at", 0.0) <= 0:
        return None
    if voice_turn.endpoint_candidate_started_at <= 0:
        return None
    return max(
        0.0,
        settings.VOICE.apple_speech_endpoint_max_delay
        - (time.monotonic() - voice_turn.endpoint_candidate_started_at),
    )


def _awaiting_stt_poll_remaining(voice_turn: VoiceInputTurn) -> float:
    """Seconds left to wait for first STT text before re-arming VAD."""
    started = voice_turn.endpoint_candidate_started_at
    if started <= 0:
        return 0.0
    return max(
        0.0,
        settings.VOICE.turn_detector_awaiting_stt_timeout - (time.monotonic() - started),
    )


def _endpoint_min_delay_remaining(voice_turn: VoiceInputTurn) -> float:
    """Apply the endpointing floor from true speech end, not after VAD fires."""
    if voice_turn.speech_ended_at <= 0:
        return settings.VOICE.turn_detector_min_delay
    return max(
        0.0,
        settings.VOICE.turn_detector_min_delay
        - (time.monotonic() - voice_turn.speech_ended_at),
    )


def _fast_recovery_target(session) -> Optional[asyncio.Task]:
    """Return the task that fast recovery should cancel, preferring accepted_input_task."""
    if session.accepted_input_task is not None and not session.accepted_input_task.done():
        return session.accepted_input_task
    if session.current_run_task is not None and not session.current_run_task.done():
        return session.current_run_task
    return None


def _is_fast_recovery_candidate(session, *, elapsed: float, window: float) -> bool:
    """Fast recovery merges quick continuations even if a short acknowledgement started playing."""
    return (
        _fast_recovery_target(session) is not None
        and elapsed < window
        and session.voice_turn is not None
    )


def _fast_recovery_miss_reason(session, *, elapsed: float, window: float) -> str | None:
    """Why speech inside the recovery window did not merge. None = not an in-window miss."""
    if elapsed >= window:
        return None
    if session.voice_turn is None:
        return "no_voice_turn"
    if _fast_recovery_target(session) is None:
        return "no_task"
    return "window_ok_but_ineligible"


def _log_fast_recovery_miss(session_id: str, session) -> None:
    elapsed = _fast_recovery_elapsed(session)
    window = settings.VOICE.fast_recovery_window
    reason = _fast_recovery_miss_reason(session, elapsed=elapsed, window=window)
    if reason is None:
        return
    logger.info(
        "Fast recovery missed | session=%s reason=%s elapsed=%.2fs window=%.1fs turn=%s",
        session_id,
        reason,
        elapsed,
        window,
        getattr(session.voice_turn, "turn_id", None),
    )


def _has_active_input_or_run(session) -> bool:
    return (
        (session.endpoint_decision_task is not None and not session.endpoint_decision_task.done())
        or (session.accepted_input_task is not None and not session.accepted_input_task.done())
        or (session.current_run_task is not None and not session.current_run_task.done())
    )


def _barge_in_candidate_active(session) -> bool:
    return (
        getattr(session, "barge_in_candidate_started_at", 0.0) > 0
        and not getattr(session, "barge_in_candidate_committed", False)
    )


def _cancel_barge_in_candidate_task(session, *, reason: str) -> None:
    task = getattr(session, "barge_in_candidate_task", None)
    current = asyncio.current_task()
    if task is not None and not task.done() and task is not current:
        task.cancel(reason)
    session.barge_in_candidate_task = None


def _cancel_barge_in_speaker_task(session, *, reason: str) -> None:
    task = getattr(session, "barge_in_speaker_task", None)
    current = asyncio.current_task()
    if task is not None and not task.done() and task is not current:
        task.cancel(reason)
    session.barge_in_speaker_task = None


def _clear_barge_in_candidate(session, *, reason: str) -> None:
    _cancel_barge_in_candidate_task(session, reason=reason)
    _cancel_barge_in_speaker_task(session, reason=reason)
    session.barge_in_candidate_started_at = 0.0
    session.barge_in_candidate_committed = False
    session.barge_in_speaker_evidence = None
    session.barge_in_speaker_attempts = 0
    if hasattr(session, "barge_in_candidate_turn"):
        session.barge_in_candidate_turn = None


def _barge_in_candidate_age(session) -> float:
    started_at = getattr(session, "barge_in_candidate_started_at", 0.0)
    return max(0.0, time.monotonic() - started_at) if started_at > 0 else 0.0


def _speaker_status_for_evidence(session) -> SpeakerMatchStatus | None:
    verifier = getattr(session, "speaker_verifier", None)
    if verifier is None or not verifier.enrolled:
        return SpeakerMatchStatus.NOT_ENROLLED
    evidence = getattr(session, "barge_in_speaker_evidence", None)
    if evidence is None:
        return None
    return evidence.status


def _barge_in_evidence(session, *, endpointed: bool = False) -> BargeInEvidence:
    candidate_turn = getattr(session, "barge_in_candidate_turn", None) or session.voice_turn
    speaker_evidence = getattr(session, "barge_in_speaker_evidence", None)
    return BargeInEvidence(
        transcript=(getattr(candidate_turn, "transcript_text", "") if candidate_turn else ""),
        candidate_age_s=_barge_in_candidate_age(session),
        endpointed=endpointed,
        proactive=bool(getattr(session, "current_trigger_instance_id", None)),
        speaker_status=_speaker_status_for_evidence(session),
        speaker_cosine=(
            speaker_evidence.cosine if isinstance(speaker_evidence, SpeakerEvidence) else None
        ),
    )


def _stamp_owner_speaker(
    voice_turn: VoiceInputTurn | None,
    evidence: SpeakerEvidence | None,
    *,
    source: str,
) -> None:
    if voice_turn is None:
        return
    if not isinstance(evidence, SpeakerEvidence) or not evidence.matched:
        return
    voice_turn.speaker_id = evidence.speaker_id
    voice_turn.speaker_confidence = evidence.cosine
    voice_turn.speaker_source = source


async def _score_followup_speaker(
    session,
    pcm: bytes,
    *,
    max_seconds: float | None = None,
) -> SpeakerEvidence:
    """One-shot owner score for follow-up identity / commit. No barge-in rescore cache."""
    verifier = getattr(session, "speaker_verifier", None)
    if verifier is None or not verifier.enrolled:
        return SpeakerEvidence(status=SpeakerMatchStatus.NOT_ENROLLED)
    threshold = settings.VOICE.barge_in_speaker_threshold
    return await asyncio.to_thread(
        verifier.verify_pcm,
        pcm,
        threshold=threshold,
        max_seconds=max_seconds,
    )


def _cancel_followup_identity_task(session, *, reason: str) -> None:
    task = getattr(session, "followup_identity_task", None)
    current = asyncio.current_task()
    if task is not None and not task.done() and task is not current:
        task.cancel(reason)
    session.followup_identity_task = None


async def _drop_followup_identity(
    session_id: str,
    session,
    *,
    reason: str,
    evidence: SpeakerEvidence | None,
) -> None:
    _cancel_followup_identity_task(session, reason=reason)
    session.processor.drop_followup_identity_candidate(reason=reason)
    session.voice_turn = None
    logger.info(
        "Follow-up identity suppressed | session=%s reason=%s speaker=%s cosine=%s",
        session_id,
        reason,
        evidence.status if evidence else None,
        evidence.cosine if evidence else None,
    )
    perf.log(
        "followup_identity_suppressed",
        session=session_id,
        source="user",
        scenario="voice",
        reason=reason,
        speaker_status=evidence.status if evidence else None,
        speaker_cosine=evidence.cosine if evidence else None,
    )


async def _open_admitted_followup(
    session_id: str,
    session,
    *,
    message_id: str,
    evidence: SpeakerEvidence,
) -> VoiceInputTurn:
    session.processor.admit_followup_identity()
    _cancel_followup_identity_task(session, reason="admitted")
    voice_turn = _ensure_voice_turn(session_id, session)
    _stamp_owner_speaker(voice_turn, evidence, source="followup")
    perf.log(
        "speech_started",
        session=session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        mode=session.processor.mode.name,
        required_frames=settings.VOICE.min_speech_frames,
        buffered_audio_ms=_pcm_duration_ms(session.processor.turn_buffer),
        speaker_status=evidence.status,
        speaker_cosine=evidence.cosine,
    )
    await manager.send_message(
        session_id,
        WSResponse(
            message_id=message_id,
            type=WSMessageType.SPEECH_START,
            data={"is_speech": True},
        ),
    )
    await _publish_voice_user_start(session_id, session)
    await _start_streaming_stt(session_id, session, bytes(session.processor.turn_buffer))
    return voice_turn


async def _resolve_followup_identity(session_id: str, session, *, message_id: str) -> bool:
    """Score onset PCM. True when the capture is now an owner turn."""
    if not session.processor.followup_identity_pending:
        return session.voice_turn is not None
    pcm = session.processor.peek_turn_speech_audio()
    evidence = await _score_followup_speaker(
        session,
        pcm,
        max_seconds=settings.VOICE.barge_in_speaker_onset_seconds,
    )
    if evidence.status is SpeakerMatchStatus.MISMATCH:
        await _drop_followup_identity(
            session_id, session, reason="speaker_mismatch", evidence=evidence
        )
        return False
    await _open_admitted_followup(
        session_id, session, message_id=message_id, evidence=evidence
    )
    return True


async def _wait_followup_identity(session_id: str, session, *, message_id: str) -> None:
    task = asyncio.current_task()
    try:
        deadline = time.monotonic() + settings.VOICE.barge_in_speaker_onset_seconds
        min_ms = MIN_SCORE_SECONDS * 1000.0
        while session.processor.followup_identity_pending:
            pcm_ms = _pcm_duration_ms(session.processor.peek_turn_speech_audio())
            phase = session.processor.turn_phase
            if pcm_ms >= min_ms or phase != SpeechTurnPhase.SPEAKING:
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        if getattr(session, "followup_identity_task", None) is not task:
            return
        if not session.processor.followup_identity_pending:
            return
        await _resolve_followup_identity(session_id, session, message_id=message_id)
    except asyncio.CancelledError:
        return
    finally:
        if getattr(session, "followup_identity_task", None) is task:
            session.followup_identity_task = None



def _speaker_perf_fields(session) -> dict:
    evidence = getattr(session, "barge_in_speaker_evidence", None)
    if not isinstance(evidence, SpeakerEvidence):
        return {
            "speaker_status": _speaker_status_for_evidence(session),
            "speaker_cosine": None,
        }
    return {
        "speaker_status": evidence.status,
        "speaker_cosine": evidence.cosine,
        "speaker_threshold": evidence.threshold,
        "speaker_id": evidence.speaker_id,
    }


def _speech_audio_ms(session) -> float:
    processor = getattr(session, "processor", None)
    peek = getattr(processor, "peek_turn_speech_audio", None)
    return _pcm_duration_ms(peek() if callable(peek) else 0)


def _log_barge_in_outcome(
    session_id: str,
    session,
    *,
    outcome: str,
    reason: str,
    turn_id: str | None,
    transcript_chars: int,
    candidate_age_ms: float,
) -> None:
    """One bounded INFO line for commit/suppress — no transcript text or raw audio."""
    speaker = _speaker_perf_fields(session)
    logger.info(
        "Barge-in %s | session=%s turn=%s reason=%s speaker=%s cosine=%s thr=%s "
        "speech_ms=%.1f age_ms=%.1f chars=%d attempts=%d",
        outcome,
        session_id,
        turn_id,
        reason,
        speaker.get("speaker_status"),
        speaker.get("speaker_cosine"),
        speaker.get("speaker_threshold"),
        _speech_audio_ms(session),
        candidate_age_ms,
        transcript_chars,
        getattr(session, "barge_in_speaker_attempts", 0),
    )


async def _ensure_barge_in_speaker_evidence(
    session,
    *,
    rescore: bool = False,
) -> SpeakerEvidence | None:
    """Score candidate speech when the owner is enrolled.

    At most two inferences per candidate. ``rescore`` replaces a prior negative
    with a fresh onset-forward embedding once more audio may have accumulated.
    """
    verifier = getattr(session, "speaker_verifier", None)
    if verifier is None or not verifier.enrolled:
        session.barge_in_speaker_evidence = None
        return None

    cached = getattr(session, "barge_in_speaker_evidence", None)
    attempts = getattr(session, "barge_in_speaker_attempts", 0)
    if isinstance(cached, SpeakerEvidence):
        if not rescore or cached.matched or attempts >= 2:
            return cached
        session.barge_in_speaker_evidence = None

    existing = getattr(session, "barge_in_speaker_task", None)
    if existing is not None and not existing.done():
        return await existing

    pcm = session.processor.peek_turn_speech_audio()
    threshold = settings.VOICE.barge_in_speaker_threshold
    started = time.perf_counter()
    attempt = attempts + 1

    async def _run() -> SpeakerEvidence:
        evidence = await asyncio.to_thread(
            verifier.verify_pcm,
            pcm,
            threshold=threshold,
            max_seconds=settings.VOICE.barge_in_speaker_onset_seconds,
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        session.barge_in_speaker_evidence = evidence
        session.barge_in_speaker_attempts = attempt
        perf.log(
            "barge_in_speaker_scored",
            speaker_status=evidence.status,
            speaker_cosine=evidence.cosine,
            speaker_threshold=evidence.threshold,
            speaker_id=evidence.speaker_id,
            inference_latency_ms=latency_ms,
            speech_audio_ms=_pcm_duration_ms(pcm),
            attempt=attempt,
            rescore=rescore,
        )
        return evidence

    task = asyncio.create_task(_run())
    session.barge_in_speaker_task = task
    try:
        return await task
    finally:
        if getattr(session, "barge_in_speaker_task", None) is task:
            session.barge_in_speaker_task = None


async def _commit_barge_in_candidate(session_id: str, session, *, reason: str) -> None:
    if getattr(session, "barge_in_candidate_committed", False):
        return
    voice_turn = getattr(session, "barge_in_candidate_turn", None) or session.voice_turn
    session.barge_in_candidate_committed = True
    _cancel_barge_in_candidate_task(session, reason=f"committed:{reason}")
    _stamp_owner_speaker(
        voice_turn,
        getattr(session, "barge_in_speaker_evidence", None),
        source="barge_in",
    )
    if voice_turn is not None:
        voice_turn.admission_source = "barge_in"
        voice_turn.admission_reason = reason
        session.voice_turn = voice_turn
    turn_id = voice_turn.turn_id if voice_turn else None
    transcript_chars = len(voice_turn.transcript_text) if voice_turn else 0
    candidate_age_ms = round(_barge_in_candidate_age(session) * 1000, 1)
    _log_barge_in_outcome(
        session_id,
        session,
        outcome="committed",
        reason=reason,
        turn_id=turn_id,
        transcript_chars=transcript_chars,
        candidate_age_ms=candidate_age_ms,
    )
    await _publish_voice_user_start(session_id, session)
    if voice_turn is not None and session.voice_turn is None:
        session.voice_turn = voice_turn
    perf.log(
        "barge_in_committed",
        session=session_id,
        turn_id=turn_id,
        source="user",
        scenario="voice",
        reason=reason,
        proactive=bool(getattr(session, "current_trigger_instance_id", None)),
        transcript_chars=transcript_chars,
        candidate_age_ms=candidate_age_ms,
        **_speaker_perf_fields(session),
    )
    await manager.send_voice_response(session_id, WSMessageType.SPEECH_START, {"is_speech": True})


async def _suppress_barge_in_candidate(session_id: str, session, *, reason: str) -> None:
    voice_turn = getattr(session, "barge_in_candidate_turn", None) or session.voice_turn
    turn_id = voice_turn.turn_id if voice_turn else None
    transcript_chars = len(voice_turn.transcript_text) if voice_turn else 0
    candidate_age_ms = round(_barge_in_candidate_age(session) * 1000, 1)
    speaker_fields = _speaker_perf_fields(session)
    _log_barge_in_outcome(
        session_id,
        session,
        outcome="suppressed",
        reason=reason,
        turn_id=turn_id,
        transcript_chars=transcript_chars,
        candidate_age_ms=candidate_age_ms,
    )
    _clear_barge_in_candidate(session, reason=f"suppressed:{reason}")
    await _close_streaming_stt(session, reason=f"barge_in_suppressed:{reason}")
    _discard_turn_latency(session_id, voice_turn, reason=f"barge_in_suppressed:{reason}")
    session.processor.suppress_barge_in_candidate(reason=reason)
    if turn_id:
        # Candidate partials use turn_id as the transcript message id.
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=generate_id(),
                type=WSMessageType.RETRACT,
                data={"message_id": turn_id},
            ),
        )
    perf.log(
        "barge_in_suppressed",
        session=session_id,
        turn_id=turn_id,
        source="user",
        scenario="voice",
        reason=reason,
        proactive=bool(getattr(session, "current_trigger_instance_id", None)),
        transcript_chars=transcript_chars,
        candidate_age_ms=candidate_age_ms,
        **speaker_fields,
    )


async def _handoff_endpointed_voice_turn(
    session_id: str,
    session,
    voice_turn: VoiceInputTurn,
) -> None:
    """Publish user-end and schedule the normal endpoint decision exactly once."""
    await event_bus.publish(
        Event(
            type=EventType.VOICE_USER_END,
            source="websocket",
            data={"session_id": session_id},
        )
    )
    _schedule_endpoint_decision(session_id, session, voice_turn)


async def _resolve_barge_in_candidate(
    session_id: str,
    session,
    *,
    endpointed: bool = False,
    handoff_on_commit: bool = True,
) -> AdmissionAction:
    if not _barge_in_candidate_active(session):
        return AdmissionAction.WAIT
    candidate_turn = getattr(session, "barge_in_candidate_turn", None) or session.voice_turn
    _sync_voice_turn_transcript(
        session_id,
        session,
        reason="barge_in_candidate",
        voice_turn=candidate_turn,
    )

    max_wait_s = settings.VOICE.barge_in_candidate_max_wait_s

    def decide(evidence: BargeInEvidence) -> AdmissionDecision:
        return decide_barge_in_admission(
            evidence,
            min_text_chars=settings.VOICE.barge_in_candidate_min_text_chars,
            min_delay_s=settings.VOICE.barge_in_candidate_min_delay_s,
            max_wait_s=max_wait_s,
        )

    evidence = _barge_in_evidence(session, endpointed=endpointed)
    decision = decide(evidence)
    if decision.reason == "speaker_pending":
        await _ensure_barge_in_speaker_evidence(session)
        decision = decide(_barge_in_evidence(session, endpointed=endpointed))
    # After max-wait, replace a first negative with one rescore over accumulated PCM.
    if (
        decision.reason in {"speaker_mismatch", "speaker_unavailable"}
        and _barge_in_candidate_age(session) >= max_wait_s
        and getattr(session, "barge_in_speaker_attempts", 0) == 1
    ):
        await _ensure_barge_in_speaker_evidence(session, rescore=True)
        decision = decide(_barge_in_evidence(session, endpointed=endpointed))

    if decision.action is AdmissionAction.COMMIT:
        await _commit_barge_in_candidate(session_id, session, reason=decision.reason)
        # Deferred soft-wait / max-wait commits land here while already endpointed.
        # Hand the admitted turn into the normal endpoint path so STT does not hang.
        voice_turn = session.voice_turn or candidate_turn
        if (
            handoff_on_commit
            and endpointed
            and voice_turn is not None
            and getattr(session.processor, "turn_phase", None)
            == SpeechTurnPhase.ENDPOINT_CANDIDATE
        ):
            await _handoff_endpointed_voice_turn(session_id, session, voice_turn)
    elif decision.action is AdmissionAction.SUPPRESS:
        await _suppress_barge_in_candidate(session_id, session, reason=decision.reason)
    return decision.action


async def _barge_in_candidate_max_wait(session_id: str, session, voice_turn: VoiceInputTurn) -> None:
    task = asyncio.current_task()
    try:
        await asyncio.sleep(settings.VOICE.barge_in_candidate_max_wait_s)
        if (
            getattr(session, "barge_in_candidate_task", None) is task
            and getattr(session, "barge_in_candidate_turn", None) is voice_turn
            and _barge_in_candidate_active(session)
        ):
            soft_endpointed = (
                getattr(session.processor, "turn_phase", None)
                == SpeechTurnPhase.ENDPOINT_CANDIDATE
            )
            await _resolve_barge_in_candidate(
                session_id, session, endpointed=soft_endpointed
            )
    except asyncio.CancelledError:
        return
    finally:
        if getattr(session, "barge_in_candidate_task", None) is task:
            session.barge_in_candidate_task = None


def _schedule_barge_in_candidate_max_wait(session_id: str, session, voice_turn: VoiceInputTurn) -> None:
    existing = getattr(session, "barge_in_candidate_task", None)
    if existing is not None and not existing.done():
        return
    session.barge_in_candidate_task = asyncio.create_task(
        _barge_in_candidate_max_wait(session_id, session, voice_turn)
    )


async def _should_commit_voice_turn(session_id: str, session) -> tuple[bool, TurnDecision]:
    """Decide whether a VAD endpoint candidate should commit the current voice turn."""
    voice_turn = session.voice_turn
    if voice_turn is None:
        return True, TurnDecision(done=True, reason="no_voice_turn")

    text = _sync_voice_turn_transcript(session_id, session, reason="turn_detector")
    text_chars = len(text)
    now = time.monotonic()

    # The max-delay backstop should apply to the current unresolved endpoint,
    # not to old pauses before the user continued speaking.
    if (
        voice_turn.endpoint_candidate_started_at <= 0
        or text_chars > voice_turn.endpoint_candidate_text_chars
    ):
        voice_turn.endpoint_candidate_started_at = now
        voice_turn.endpoint_candidate_text_chars = text_chars

    endpoint_age = now - voice_turn.endpoint_candidate_started_at
    if not text and session.stt_stream is not None and endpoint_age < settings.VOICE.turn_detector_max_delay:
        perf.log(
            "turn_detector_decision",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
            decision="continue",
            reason="awaiting_stt_text",
            text_chars=text_chars,
            endpoint_age_ms=round(endpoint_age * 1000, 1),
        )
        return False, TurnDecision(done=False, reason="awaiting_stt_text")

    # Wake opens attention + seeds preroll; wake-only / empty request is not a finished turn.
    if voice_turn.from_wake and not _wake_turn_has_request(text):
        if endpoint_age < settings.VOICE.turn_detector_max_delay:
            perf.log(
                "turn_detector_decision",
                session=session_id,
                turn_id=voice_turn.turn_id,
                source="user",
                scenario="voice",
                decision="continue",
                reason="wake_followon_pending",
                text_chars=text_chars,
                endpoint_age_ms=round(endpoint_age * 1000, 1),
                from_wake=True,
            )
            return False, TurnDecision(done=False, reason="wake_followon_pending")
        perf.log(
            "turn_detector_decision",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
            decision="settle",
            reason="wake_followon_timeout",
            text_chars=text_chars,
            endpoint_age_ms=round(endpoint_age * 1000, 1),
            from_wake=True,
        )
        return True, TurnDecision(done=True, reason="wake_followon_timeout")

    if not text:
        reason = "max_delay:awaiting_stt_text"
        perf.log(
            "turn_detector_decision",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
            decision="commit",
            reason=reason,
            text_chars=text_chars,
            endpoint_age_ms=round(endpoint_age * 1000, 1),
        )
        return True, TurnDecision(done=True, reason=reason)

    transcript_is_stable, transcript_age = _apple_speech_transcript_stability(session, now)
    if not transcript_is_stable and endpoint_age < settings.VOICE.turn_detector_max_delay:
        perf.log(
            "turn_detector_decision",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
            decision="continue",
            reason="streaming_transcript_unstable",
            text_chars=text_chars,
            endpoint_age_ms=round(endpoint_age * 1000, 1),
            transcript_age_ms=round((transcript_age or 0.0) * 1000, 1),
        )
        return False, TurnDecision(done=False, reason="streaming_transcript_unstable")

    perf.start(
        "turn_detector",
        session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        text_chars=text_chars,
        backend="audio_eou",
    )
    decision = await _ensure_turn_detector(session).predict(language="en")
    perf.end(
        "turn_detector",
        session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        text_chars=text_chars,
        decision="commit" if decision.done else "continue",
        reason=decision.reason,
        confidence=round(decision.confidence, 4) if decision.confidence is not None else None,
    )
    if decision.done:
        perf.log(
            "turn_detector_decision",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
            decision="commit",
            reason=decision.reason,
            confidence=round(decision.confidence, 4) if decision.confidence is not None else None,
            text_chars=text_chars,
            endpoint_age_ms=round(endpoint_age * 1000, 1),
        )
        return True, decision

    local_wait_remaining = _apple_speech_endpoint_wait_remaining(session, voice_turn)
    if local_wait_remaining is not None:
        if local_wait_remaining > 0:
            perf.log(
                "turn_detector_decision",
                session=session_id,
                turn_id=voice_turn.turn_id,
                source="user",
                scenario="voice",
                decision="continue",
                reason=f"local_endpoint_wait:{decision.reason}",
                confidence=round(decision.confidence, 4) if decision.confidence is not None else None,
                text_chars=text_chars,
                endpoint_age_ms=round(endpoint_age * 1000, 1),
                remaining_ms=round(local_wait_remaining * 1000, 1),
            )
            return False, TurnDecision(
                done=False,
                confidence=decision.confidence,
                reason=f"local_endpoint_wait:{decision.reason}",
            )
        perf.log(
            "turn_detector_decision",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
            decision="commit",
            reason=f"local_endpoint_max_delay:{decision.reason}",
            confidence=round(decision.confidence, 4) if decision.confidence is not None else None,
            text_chars=text_chars,
            endpoint_age_ms=round(endpoint_age * 1000, 1),
        )
        return True, TurnDecision(
            done=True,
            confidence=decision.confidence,
            reason=f"local_endpoint_max_delay:{decision.reason}",
        )

    if endpoint_age >= settings.VOICE.turn_detector_max_delay:
        perf.log(
            "turn_detector_decision",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
            decision="commit",
            reason=f"max_delay:{decision.reason}",
            confidence=round(decision.confidence, 4) if decision.confidence is not None else None,
            text_chars=text_chars,
            endpoint_age_ms=round(endpoint_age * 1000, 1),
        )
        return True, TurnDecision(
            done=True,
            confidence=decision.confidence,
            reason=f"max_delay:{decision.reason}",
        )

    perf.log(
        "turn_detector_decision",
        session=session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        decision="continue",
        reason=decision.reason,
        confidence=round(decision.confidence, 4) if decision.confidence is not None else None,
        text_chars=text_chars,
        endpoint_age_ms=round(endpoint_age * 1000, 1),
    )
    return False, decision


async def _finish_streaming_stt(
    session_id: str,
    session,
    *,
    voice_turn: VoiceInputTurn | None = None,
) -> tuple[str | None, dict[str, int | bool]]:
    coordinator = session.stt_stream
    session.stt_stream = None
    if coordinator is None:
        return None, {}
    raw_transcript = await coordinator.finish()
    stt_stats = {
        "bytes_fed": coordinator.bytes_fed,
        "feed_count": coordinator.feed_count,
    }
    voice_turn = voice_turn or session.voice_turn
    transcript = (
        _merge_continuation_text(voice_turn.continuation_prefix, raw_transcript or "")
        if voice_turn
        else raw_transcript
    )
    perf.log(
        "stt_stream_finished",
        session=session_id,
        turn_id=voice_turn.turn_id if voice_turn else None,
        stream_id=coordinator.stream_id,
        source="user",
        scenario="voice",
        transcript_chars=len(transcript or ""),
        raw_transcript_chars=len(raw_transcript or ""),
        voice_turn_chars=len(voice_turn.transcript_text) if voice_turn else 0,
        delta_vs_voice_turn=len(transcript or "") - (len(voice_turn.transcript_text) if voice_turn else 0),
        regressed_vs_voice_turn=bool(voice_turn and transcript and len(transcript) < len(voice_turn.transcript_text)),
        had_transcript=bool(transcript),
        **stt_stats,
    )
    return transcript, stt_stats


async def _close_streaming_stt(session, *, reason: str) -> None:
    coordinator = session.stt_stream
    session.stt_stream = None
    if coordinator is None:
        return
    await coordinator.close(reason=reason)


async def _commit_provider_turn_end(session_id: str, session, voice_turn: VoiceInputTurn) -> None:
    task = asyncio.current_task()
    if session.voice_turn is not voice_turn:
        return
    if _barge_in_candidate_active(session):
        await _resolve_barge_in_candidate(session_id, session, endpointed=True)
        return
    _cancel_endpoint_decision(session, reason="provider_turn_end")
    session.accepted_input_task = task
    try:
        turn_decision = TurnDecision(done=True, reason="provider_eou")
        if resolve_voice_config_sync().stt_provider == "apple_speech":
            should_commit, turn_decision = await _should_commit_voice_turn(session_id, session)
            if not should_commit:
                voice_turn.continue_count += 1
                if turn_decision.reason == "awaiting_stt_text":
                    voice_turn.awaiting_stt_count += 1
                session.processor.continue_turn(reason=turn_decision.reason)
                return
            if turn_decision.reason == "wake_followon_timeout":
                await _settle_wake_followon(
                    session_id,
                    session,
                    voice_turn,
                    reason=turn_decision.reason,
                )
                return
        await _commit_voice_turn(
            session_id,
            session,
            voice_turn,
            turn_decision,
        )
    except Exception:
        logger.error("Provider EOU commit failed for %s", session_id, exc_info=True)
    finally:
        if session.accepted_input_task is task:
            session.accepted_input_task = None


def _cancel_endpoint_decision(session, *, reason: str) -> None:
    task = getattr(session, "endpoint_decision_task", None)
    if task is not None and not task.done():
        task.cancel(reason)
    session.endpoint_decision_task = None


def _endpoint_task_is_current(session, voice_turn: VoiceInputTurn, task: asyncio.Task | None) -> bool:
    return (
        task is not None
        and session.endpoint_decision_task is task
        and session.voice_turn is voice_turn
        and session.processor.turn_phase == SpeechTurnPhase.ENDPOINT_CANDIDATE
    )


async def _suppress_finalized_followup(
    session_id: str,
    session,
    voice_turn: VoiceInputTurn,
    *,
    reason: str,
) -> None:
    """Discard a finalized follow-up that admission suppressed (future DDSD path)."""
    turn_id = voice_turn.turn_id
    await _close_streaming_stt(session, reason=f"followup_suppressed:{reason}")
    await _close_turn_detector(session)
    _discard_voice_turn_latency(session_id, session, reason=f"followup_suppressed:{reason}")
    if turn_id:
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=generate_id(),
                type=WSMessageType.RETRACT,
                data={"message_id": turn_id},
            ),
        )
    if session.processor.mode != VoiceMode.PASSIVE:
        session.processor.force_active(reason=f"followup_suppressed:{reason}")
    await manager.send_voice_response(
        session_id,
        WSMessageType.STATUS,
        {"stage": "listening"},
    )
    perf.log(
        "turn_admission_suppressed",
        session=session_id,
        turn_id=turn_id,
        source="user",
        scenario="voice",
        admission_source="followup",
        admission_reason=reason,
        transcript_chars=len(voice_turn.transcript_text),
    )


async def _commit_voice_turn(
    session_id: str,
    session,
    voice_turn: VoiceInputTurn,
    turn_decision: TurnDecision,
) -> None:
    # Anchor end_of_turn_delay at commit decision, before STT finalize wait.
    commit_started_at = time.monotonic()
    if getattr(session, "barge_in_candidate_committed", False):
        _clear_barge_in_candidate(session, reason="voice_turn_commit")
    _flush_turn_detector(session)
    needs_followup_score = (
        voice_turn.admission_source is None and not voice_turn.from_wake
    )
    speech_pcm = b""
    if needs_followup_score:
        peek = getattr(session.processor, "peek_turn_speech_audio", None)
        speech_pcm = peek() if callable(peek) else b""
    turn_audio = session.processor.consume_turn_audio()
    voice_turn.last_endpoint_monotonic = time.monotonic()
    streamed_transcript, stt_stats = await _finish_streaming_stt(
        session_id,
        session,
        voice_turn=voice_turn,
    )
    if streamed_transcript is None:
        voice_config = resolve_voice_config_sync()
        stt_stats = {"bytes_fed": 0, "feed_count": 0}
        perf.log(
            "stt_stream_missing",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
            audio_ms=_pcm_duration_ms(turn_audio),
            backend=voice_config.stt_provider,
        )
    if streamed_transcript:
        _apply_voice_turn_transcript(
            session_id,
            voice_turn,
            streamed_transcript,
            event="stt_final_transcript_applied",
            reason="stream_finish",
        )
    if not voice_turn.transcript_text:
        logger.warning("Voice turn committed without streaming transcript; dropping turn.")
        await manager.send_voice_response(session_id, WSMessageType.STATUS, {"stage": "listening"})
        return
    # Merge visibility into turn_detection; omit endpoint_age_ms so the earlier
    # decision-time value from _should_commit_voice_turn is preserved.
    perf.log(
        "turn_detector_decision",
        session=session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        decision="commit",
        reason=turn_decision.reason,
        confidence=round(turn_decision.confidence, 4) if turn_decision.confidence is not None else None,
        text_chars=len(voice_turn.transcript_text),
        **_eou_visibility_fields(voice_turn, now=commit_started_at),
        **_endpointing_snapshot(),
    )
    if await _handle_local_voice_command(session_id, session, voice_turn):
        return

    if voice_turn.admission_source is None:
        if voice_turn.from_wake:
            voice_turn.admission_source = "wake"
            voice_turn.admission_reason = "wake_word"
        else:
            speaker_evidence = await _score_followup_speaker(session, speech_pcm)
            admission = decide_followup_admission(
                FollowupEvidence(
                    transcript=voice_turn.transcript_text,
                    speaker_status=speaker_evidence.status,
                    directedness=Directedness.UNKNOWN,
                )
            )
            if admission.action is AdmissionAction.SUPPRESS:
                await _suppress_finalized_followup(
                    session_id,
                    session,
                    voice_turn,
                    reason=admission.reason,
                )
                return
            voice_turn.admission_source = "followup"
            voice_turn.admission_reason = admission.reason
            _stamp_owner_speaker(voice_turn, speaker_evidence, source="followup")

    coverage = _stt_coverage_fields(len(turn_audio), int(stt_stats.get("bytes_fed", 0)))
    perf.log(
        "voice_turn_committed",
        session=session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        reason=turn_decision.reason,
        confidence=round(turn_decision.confidence, 4) if turn_decision.confidence is not None else None,
        audio_ms=coverage["turn_audio_ms"],
        transcript_chars=len(voice_turn.transcript_text),
        used_streaming_transcript=bool(voice_turn.transcript_text),
        stt_feed_count=stt_stats.get("feed_count"),
        admission_source=voice_turn.admission_source,
        admission_reason=voice_turn.admission_reason,
        **coverage,
    )
    # Single live stdout line per turn — reveals START drops (audio side): a gap above
    # one frame (~96ms) means captured PCM never reached STT.
    logger.info(
        "STT coverage | captured=%sms fed=%sms gap=%sms cov=%s%% feeds=%s",
        coverage["turn_audio_ms"],
        coverage["stt_bytes_fed_ms"],
        coverage["stt_audio_gap_ms"],
        coverage["stt_coverage_pct"],
        stt_stats.get("feed_count"),
    )
    perf.start(
        "response_latency",
        session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
    )

    AssistantOrchestrator._set_current_run_task(
        session,
        asyncio.create_task(
            orchestrator.process_turn(
                connection_id=session_id,
                audio_bytes=turn_audio,
                text=voice_turn.transcript_text or None,
                turn_id=voice_turn.turn_id,
                attachments=_drain_attachments(session),
            )
        )
    )
    perf.log(
        "process_turn_scheduled",
        session=session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        audio_ms=_pcm_duration_ms(turn_audio),
        transcript_chars=len(voice_turn.transcript_text),
    )

async def _handle_local_voice_command(session_id: str, session, voice_turn: VoiceInputTurn) -> bool:
    if await _resolve_pending_confirmation(session_id, session, voice_turn.transcript_text):
        return True
    command = resolve_local_command(
        voice_turn.transcript_text,
        soft_muted=bool(getattr(session, "soft_muted", False)),
    )
    if command is LocalVoiceCommand.NONE:
        return False

    prior_soft_muted = bool(getattr(session, "soft_muted", False))
    stage = "idle"
    local_ack = _LOCAL_UNMUTE_ACK
    owner_id = getattr(session, "owner_id", session_id)
    node_id = getattr(getattr(session, "presence", None), "node_id", None)
    attention_state: AttentionState | None = None

    if command is LocalVoiceCommand.UNMUTE:
        try:
            attention_state = await _set_attention_mode_fast_path(owner_id, node_id, "active")
        except Exception:
            logger.warning("attention mode update failed for UNMUTE; proceeding with session-local state only")
        clear_soft_mute_for_session(session)
        session.processor.force_active(reason="local_command.unmute")
        stage = "listening"
    elif command is LocalVoiceCommand.SOFT_MUTE:
        try:
            attention_state = await _set_attention_mode_fast_path(owner_id, node_id, "quiet")
        except Exception:
            logger.warning("attention mode update failed for SOFT_MUTE; proceeding with session-local state only")
        apply_soft_mute_for_session(session, reason="local_command.soft_mute")
        await manager.send_voice_response(session_id, WSMessageType.STOP, {"reason": "local_mute"})
    elif command is LocalVoiceCommand.POWER_ON:
        try:
            attention_state = await _set_attention_mode_fast_path(owner_id, node_id, "active")
        except Exception:
            logger.warning("attention mode update failed for POWER_ON; proceeding with session-local state only")
        clear_soft_mute_for_session(session)
        session.processor.force_active(reason="local_command.power_on")
        stage = "listening"
    elif command is LocalVoiceCommand.POWER_CHECK:
        was_powered_down = False
        try:
            was_powered_down = await _get_attention_mode_fast_path(owner_id) == "paused"
        except Exception:
            logger.warning("attention mode read failed for POWER_CHECK; falling back to normal power-on")
        if not was_powered_down and not prior_soft_muted:
            return False
        try:
            attention_state = await _set_attention_mode_fast_path(owner_id, node_id, "active")
        except Exception:
            logger.warning("attention mode update failed for POWER_CHECK; proceeding with session-local state only")
        clear_soft_mute_for_session(session)
        session.processor.force_active(reason="local_command.power_check")
        stage = "listening"
        if was_powered_down:
            local_ack = _LOCAL_POWER_CHECK_ACK
    elif command is LocalVoiceCommand.POWER_DOWN:
        try:
            attention_state = await _set_attention_mode_fast_path(owner_id, node_id, "paused")
        except Exception:
            logger.warning("attention mode update failed for POWER_DOWN; proceeding with session-local state only")
        apply_soft_mute_for_session(session, reason="local_command.power_down")
        await manager.send_voice_response(session_id, WSMessageType.STOP, {"reason": "local_power_down"})
    elif command in {LocalVoiceCommand.STOP_LISTENING, LocalVoiceCommand.DROP}:
        session.processor.force_passive(reason=f"local_command.{command.value}")
    elif command is LocalVoiceCommand.STOP:
        from core.triggers.service import trigger_service
        await trigger_service.acknowledge_latest_for_owner(session.owner_id)
        session.processor.force_passive(reason=f"local_command.{command.value}")
        await manager.send_voice_response(session_id, WSMessageType.STOP, {"reason": "local_stop"})
    elif command in {LocalVoiceCommand.ACKNOWLEDGE, LocalVoiceCommand.SNOOZE}:
        from core.triggers.service import trigger_service
        if command is LocalVoiceCommand.ACKNOWLEDGE:
            await trigger_service.acknowledge_latest_for_owner(session.owner_id)
        else:
            from datetime import timedelta
            await trigger_service.snooze_latest_for_owner(
                session.owner_id,
                duration=timedelta(minutes=10),
            )
        await manager.send_voice_response(session_id, WSMessageType.STOP, {"reason": "notification_ack"})

    perf.log(
        "local_voice_command",
        session=session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        command=command.value,
        prior_soft_muted=prior_soft_muted,
        soft_muted=bool(getattr(session, "soft_muted", False)),
        transcript_chars=len(voice_turn.transcript_text),
    )
    attention_update = (
        {"attention": attention_state_payload(attention_state)}
        if attention_state is not None
        else {}
    )
    session_update = {"session": session_state_payload(session)}
    if command in {LocalVoiceCommand.UNMUTE, LocalVoiceCommand.POWER_ON, LocalVoiceCommand.POWER_CHECK}:
        session.voice_turn = None
        await manager.send_voice_response(
            session_id,
            WSMessageType.STATUS,
            {**attention_update, **session_update},
        )
        await orchestrator._deliver_text(
            session_id,
            local_ack,
            None,
            delivery="local_command",
            persist=False,
        )
    else:
        _discard_voice_turn_latency(session_id, session, reason=f"local_command:{command.value}")
        await manager.send_voice_response(
            session_id,
            WSMessageType.STATUS,
            {"stage": stage, **attention_update, **session_update},
        )
    return True


async def _resolve_endpoint_candidate(session_id: str, session, voice_turn: VoiceInputTurn) -> None:
    task = asyncio.current_task()
    # Bind owner/connection so endpoint-phase stages (turn_detector, batch STT)
    # attach to the turn summary — otherwise perf drops them and the panel loses
    # the whole "Listen" stage.
    perf_token = perf.bind_context(
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        owner_id=getattr(session, "owner_id", session_id),
        connection_id=getattr(session, "connection_id", session_id),
    )
    try:
        await asyncio.sleep(_endpoint_min_delay_remaining(voice_turn))
        if not _endpoint_task_is_current(session, voice_turn, task):
            return
        while True:
            should_commit, turn_decision = await _should_commit_voice_turn(session_id, session)
            if not _endpoint_task_is_current(session, voice_turn, task):
                return
            if should_commit:
                break
            if turn_decision.reason == "awaiting_stt_text":
                # Poll in-candidate for late streaming text. continue_turn resets the
                # VAD silence clock and forces another full endpoint cycle.
                remaining = _awaiting_stt_poll_remaining(voice_turn)
                if remaining > 0:
                    await asyncio.sleep(max(0.02, min(remaining, 0.08)))
                    if not _endpoint_task_is_current(session, voice_turn, task):
                        return
                    continue
            elif turn_decision.reason == "wake_followon_pending":
                await asyncio.sleep(0.05)
                if not _endpoint_task_is_current(session, voice_turn, task):
                    return
                continue
            elif turn_decision.reason == "streaming_transcript_unstable":
                await asyncio.sleep(settings.VOICE.apple_speech_commit_stability_delay)
                if not _endpoint_task_is_current(session, voice_turn, task):
                    return
                continue
            elif turn_decision.reason.startswith("local_endpoint_wait:"):
                remaining = _apple_speech_endpoint_wait_remaining(session, voice_turn)
                await asyncio.sleep(max(0.02, min(remaining or 0.05, 0.08)))
                if not _endpoint_task_is_current(session, voice_turn, task):
                    return
                continue
            if not should_commit:
                break

        if not should_commit:
            voice_turn.continue_count += 1
            if turn_decision.reason == "awaiting_stt_text":
                voice_turn.awaiting_stt_count += 1
            session.processor.continue_turn(reason="turn_detector_continue")
            if settings.VOICE.trace_voice_events:
                perf.log(
                    "voice_turn_continued",
                    session=session_id,
                    turn_id=voice_turn.turn_id,
                    source="user",
                    scenario="voice",
                    reason=turn_decision.reason,
                    confidence=round(turn_decision.confidence, 4) if turn_decision.confidence is not None else None,
                    buffered_audio_ms=_pcm_duration_ms(session.processor.turn_buffer),
                    transcript_chars=len(voice_turn.transcript_text),
                )
            return

        if turn_decision.reason == "wake_followon_timeout":
            session.accepted_input_task = task
            await _settle_wake_followon(
                session_id,
                session,
                voice_turn,
                reason=turn_decision.reason,
            )
            return

        session.accepted_input_task = task
        await _commit_voice_turn(session_id, session, voice_turn, turn_decision)
    except asyncio.CancelledError:
        perf.log(
            "endpoint_candidate_cancelled",
            session=session_id,
            turn_id=voice_turn.turn_id,
            source="user",
            scenario="voice",
        )
    except Exception:
        logger.error("Endpoint candidate resolution failed for %s", session_id, exc_info=True)
    finally:
        perf.reset_context(perf_token)
        if session.endpoint_decision_task is task:
            session.endpoint_decision_task = None
        if session.accepted_input_task is task:
            session.accepted_input_task = None


def _schedule_endpoint_decision(session_id: str, session, voice_turn: VoiceInputTurn) -> None:
    existing = session.endpoint_decision_task
    if existing is not None and not existing.done():
        return
    if voice_turn.endpoint_candidate_started_at <= 0:
        voice_turn.endpoint_candidate_started_at = time.monotonic()
        voice_turn.endpoint_candidate_text_chars = len(voice_turn.transcript_text)
    session.endpoint_decision_task = asyncio.create_task(
        _resolve_endpoint_candidate(session_id, session, voice_turn)
    )


def _fast_recovery_elapsed(session) -> float:
    voice_turn = session.voice_turn
    return (
        time.monotonic() - voice_turn.last_endpoint_monotonic
        if voice_turn and voice_turn.last_endpoint_monotonic > 0
        else float("inf")
    )


async def _maybe_start_fast_recovery(
    session_id: str,
    session,
    *,
    message_id: str,
) -> bool:
    """Resume an existing user turn before treating speech-over-AI as barge-in."""
    fast_recovery_window = settings.VOICE.fast_recovery_window
    elapsed = _fast_recovery_elapsed(session)
    if not _is_fast_recovery_candidate(session, elapsed=elapsed, window=fast_recovery_window):
        return False

    voice_turn = session.voice_turn
    logger.info("Fast recovery: user resumed %.2fs after endpoint.", elapsed)
    prior_prefix = voice_turn.continuation_prefix
    prior_transcript = voice_turn.transcript_text
    cancelled_delivery = signal_current_delivery_cancel(session)
    retract_response_id = cancelled_delivery.response_id
    target = _fast_recovery_target(session)
    input_commit_in_flight = target is session.accepted_input_task
    if input_commit_in_flight:
        with contextlib.suppress(asyncio.CancelledError):
            await target
        run_task = session.current_run_task
        if run_task is not None and not run_task.done():
            run_task.cancel("fast_recovery")
    elif target is not None:
        target.cancel("fast_recovery")

    voice_turn.continuation_prefix = voice_turn.transcript_text
    if input_commit_in_flight and voice_turn.transcript_text:
        await _send_partial_transcript(session_id, voice_turn, voice_turn.transcript_text)
    _voice_trace(
        session_id,
        "fast_recovery",
        turn_id=voice_turn.turn_id,
        elapsed_ms=round(elapsed * 1000, 1),
        input_commit_in_flight=input_commit_in_flight,
        prior_prefix_tail=_text_tail(prior_prefix, 80) if prior_prefix else None,
        prior_transcript_tail=_text_tail(prior_transcript, 80) if prior_transcript else None,
        saved_prefix_tail=_text_tail(voice_turn.continuation_prefix, 80)
        if voice_turn.continuation_prefix
        else None,
        buffer_ms=_pcm_duration_ms(session.processor.turn_buffer),
    )
    voice_turn.endpoint_candidate_started_at = 0.0
    voice_turn.endpoint_candidate_text_chars = len(voice_turn.transcript_text)
    perf.log(
        "fast_recovery_triggered",
        session=session_id,
        turn_id=voice_turn.turn_id,
        source="user",
        scenario="voice",
        owner_id=getattr(session, "owner_id", None),
        connection_id=getattr(session, "connection_id", session_id),
        elapsed_ms=round(elapsed * 1000, 1),
        continuation_audio_ms=_pcm_duration_ms(session.processor.turn_buffer),
        transcript_chars=len(voice_turn.transcript_text),
        transcript_tail=_text_tail(voice_turn.transcript_text),
        active_stream_id=getattr(session.stt_stream, "stream_id", None),
        had_response_to_retract=bool(retract_response_id),
    )
    if retract_response_id:
        await manager.send_message(
            session_id,
            WSResponse(
                message_id=message_id,
                type=WSMessageType.RETRACT,
                data={
                    "response_id": retract_response_id,
                    "turn_id": voice_turn.turn_id,
                },
            ),
        )
    await manager.send_message(
        session_id,
        WSResponse(
            message_id=message_id,
            type=WSMessageType.SPEECH_START,
            data={"is_speech": True},
        ),
    )
    return True


async def _notify_setup_required(session_id: str, exc: SetupNotReadyError) -> None:
    await manager.send_message(
        session_id,
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


@register_handler(WSMessageType.USER_TEXT)
async def handle_text_input(session_id: str, message: WSMessage) -> None:
    """Handle text input — bypass SpeechProcessor/STT, route directly to orchestrator."""
    session = manager.get_session(session_id)
    if not session:
        return

    try:
        require_llm_ready()
    except SetupNotReadyError as exc:
        await _notify_setup_required(session_id, exc)
        return

    text = message.data.get("text", "").strip()
    if len(text) > MAX_TEXT_INPUT_LENGTH:
        return
    if not text and not session.pending_attachments:
        return

    if _has_active_input_or_run(session):
        logger.info("Ignoring text input while session has active input/run: session=%s", session_id)
        return

    if await _resolve_pending_confirmation(session_id, session, text):
        return

    AssistantOrchestrator._set_current_run_task(
        session,
        asyncio.create_task(
            orchestrator.process_turn(
                connection_id=session_id,
                audio_bytes=None,
                text=text,
                attachments=_drain_attachments(session),
            )
        )
    )


# --- Attachment Handler ---
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MB
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


@register_handler(WSMessageType.USER_ATTACHMENT)
async def handle_attachment(session_id: str, message: WSMessage) -> None:
    """Buffer an image attachment on the session for the next turn."""
    session = manager.get_session(session_id)
    if not session:
        return

    if message.data.get("clear"):
        session.pending_attachments.clear()
        return

    data = message.data.get("data", "")
    mime_type = message.data.get("mime_type", "image/jpeg")

    if mime_type not in ALLOWED_IMAGE_TYPES:
        logger.warning(f"Rejected attachment with mime_type={mime_type} from {session_id}")
        return

    if len(data) > MAX_ATTACHMENT_BYTES:
        logger.warning(f"Rejected oversized attachment ({len(data)} bytes) from {session_id}")
        return

    data_url = f"data:{mime_type};base64,{data}"
    session.pending_attachments.append({
        "type": "image_url",
        "image_url": {"url": data_url},
    })
    logger.debug(f"Buffered attachment for {session_id} ({mime_type}, {len(data)} chars). Total pending: {len(session.pending_attachments)}")


# --- Audio Stream Handler ---
@register_handler(WSMessageType.USER_AUDIO)
async def handle_audio_stream(session_id: str, message: WSMessage) -> None:
    """
    Pass audio to the session's SpeechProcessor and trigger orchestration on turn completion.
    """
    try:
        session = manager.get_session(session_id)
        if not session:
            return

        # Decode audio
        audio_data = message.data.get("audio")
        if not audio_data:
            return
        
        audio_bytes = (
            base64.b64decode(audio_data)
            if message.data.get("encoding") == "base64"
            else audio_data
        )
        session.ingest_voice_sample(audio_bytes)
        if session.voice_sample_buffer is not None:
            return

        # 1. Process with SpeechProcessor. While soft-muted, keep a short pre-roll so the
        # wake word is included in STT context without retaining ambient conversation.
        soft_muted = bool(getattr(session, "soft_muted", False))
        event = await session.processor.add_audio(
            audio_bytes,
            retain_preroll=True,
            preroll_seconds=settings.VOICE.soft_mute_preroll_seconds if soft_muted else None,
        )

        # 2. Handle Speech Events
        if event == SpeechEvent.WAKE_WORD_DETECTED:
            tts.prepare_for_turn()
            voice_turn = _ensure_voice_turn(session_id, session)
            voice_turn.from_wake = True
            voice_turn.admission_source = "wake"
            voice_turn.admission_reason = "wake_word"
            perf.log(
                "wake_word_detected",
                session=session_id,
                turn_id=voice_turn.turn_id,
                source="user",
                scenario="voice",
                buffered_audio_ms=_pcm_duration_ms(session.processor.turn_buffer),
            )
            await event_bus.publish(
                Event(
                    type=EventType.VOICE_WAKE,
                    source="websocket",
                    data={"session_id": session_id}
                )
            )
            # Notify Frontend to wake up
            await manager.send_message(
                session_id,
                WSResponse(
                    message_id=message.id,
                    type=WSMessageType.SPEECH_START, # Reuse speech start to trigger UI
                    data={"is_speech": True, "wake_word": True}
                )
            )
            await _start_streaming_stt(session_id, session, bytes(session.processor.turn_buffer))

        elif event == SpeechEvent.SESSION_ENDED:
            _cancel_followup_identity_task(session, reason="session_ended")
            _cancel_endpoint_decision(session, reason="session_ended")
            await _close_streaming_stt(session, reason="session_ended")
            await _close_turn_detector(session)
            _discard_voice_turn_latency(session_id, session, reason="session_ended")
            # Delegate to orchestrator — handles force_passive + frontend notification
            await event_bus.publish(
                Event(
                    type=EventType.VOICE_TIMEOUT,
                    source="websocket",
                    data={"session_id": session_id}
                )
            )

        elif event == SpeechEvent.BARGE_IN_CANDIDATE_STARTED:
            if await _maybe_start_fast_recovery(session_id, session, message_id=message.id):
                await _start_streaming_stt(
                    session_id,
                    session,
                    bytes(session.processor.turn_buffer),
                    voice_turn=session.voice_turn,
                )
                return

            voice_turn = _create_voice_turn(session_id)
            session.barge_in_candidate_turn = voice_turn
            session.barge_in_candidate_started_at = time.monotonic()
            session.barge_in_candidate_committed = False
            session.barge_in_speaker_evidence = None
            session.barge_in_speaker_attempts = 0
            _cancel_barge_in_speaker_task(session, reason="candidate_started")
            perf.log(
                "barge_in_candidate_started",
                session=session_id,
                turn_id=voice_turn.turn_id,
                source="user",
                scenario="voice",
                proactive=bool(getattr(session, "current_trigger_instance_id", None)),
                required_frames=settings.VOICE.barge_in_min_frames,
                buffered_audio_ms=_pcm_duration_ms(session.processor.turn_buffer),
                speaker_enrolled=bool(
                    getattr(session, "speaker_verifier", None)
                    and session.speaker_verifier.enrolled
                ),
            )
            await manager.send_message(
                session_id,
                WSResponse(
                    message_id=message.id,
                    type=WSMessageType.SPEECH_START,
                    data={"is_speech": True, "barge_candidate": True},
                ),
            )
            await _start_streaming_stt(
                session_id,
                session,
                bytes(session.processor.turn_buffer),
                voice_turn=voice_turn,
            )
            _schedule_barge_in_candidate_max_wait(session_id, session, voice_turn)

        elif event == SpeechEvent.FOLLOWUP_CANDIDATE_STARTED:
            if await _maybe_start_fast_recovery(session_id, session, message_id=message.id):
                session.processor.admit_followup_identity(source="fast_recovery")
                await _start_streaming_stt(
                    session_id,
                    session,
                    bytes(session.processor.turn_buffer),
                    voice_turn=session.voice_turn,
                )
                return
            _log_fast_recovery_miss(session_id, session)
            _cancel_followup_identity_task(session, reason="candidate_started")
            session.followup_identity_task = asyncio.create_task(
                _wait_followup_identity(session_id, session, message_id=message.id)
            )

        elif event == SpeechEvent.USER_TURN_STARTED:
            elapsed = _fast_recovery_elapsed(session)
            if not await _maybe_start_fast_recovery(session_id, session, message_id=message.id):
                _log_fast_recovery_miss(session_id, session)
                has_active_endpoint = (
                    session.endpoint_decision_task is not None
                    and not session.endpoint_decision_task.done()
                )
                has_active_turn = (
                    (session.accepted_input_task and not session.accepted_input_task.done())
                    or (session.current_run_task and not session.current_run_task.done())
                    or has_active_endpoint
                )
                if has_active_turn:
                    await _publish_voice_user_start(session_id, session)
                    voice_turn = _start_new_voice_turn(session_id, session)
                else:
                    voice_turn = _ensure_voice_turn(session_id, session)
                perf.log(
                    "speech_started",
                    session=session_id,
                    turn_id=voice_turn.turn_id,
                    source="user",
                    scenario="voice",
                    mode=session.processor.mode.name,
                    required_frames=settings.VOICE.min_speech_frames,
                    buffered_audio_ms=_pcm_duration_ms(session.processor.turn_buffer),
                    fast_recovery_elapsed_ms=None if elapsed == float("inf") else round(elapsed * 1000, 1),
                    first_audio_sent=session.first_audio_sent,
                )
                await manager.send_message(
                    session_id,
                    WSResponse(
                        message_id=message.id,
                        type=WSMessageType.SPEECH_START,
                        data={"is_speech": True}
                    )
                )
                if not has_active_turn:
                    await _publish_voice_user_start(session_id, session)
            await _start_streaming_stt(session_id, session, bytes(session.processor.turn_buffer))

        elif event == SpeechEvent.TURN_RESUMED:
            if session.processor.followup_identity_pending and session.voice_turn is None:
                return
            voice_turn = (
                getattr(session, "barge_in_candidate_turn", None)
                if _barge_in_candidate_active(session)
                else _ensure_voice_turn(session_id, session)
            )
            _cancel_endpoint_decision(session, reason="speech_resumed")
            voice_turn.endpoint_candidate_started_at = 0.0
            voice_turn.endpoint_candidate_text_chars = len(voice_turn.transcript_text)
            voice_turn.speech_ended_at = 0.0
            await _feed_streaming_stt(session, audio_bytes)
            if _barge_in_candidate_active(session):
                await _resolve_barge_in_candidate(session_id, session, endpointed=False)
            perf.log(
                "voice_turn_resumed",
                session=session_id,
                turn_id=voice_turn.turn_id,
                source="user",
                scenario="voice",
                buffered_audio_ms=_pcm_duration_ms(session.processor.turn_buffer),
                transcript_chars=len(voice_turn.transcript_text),
            )

        elif event == SpeechEvent.TURN_COMPLETE:
            if session.processor.followup_identity_pending:
                _cancel_followup_identity_task(session, reason="endpoint")
                if not await _resolve_followup_identity(
                    session_id, session, message_id=message.id
                ):
                    return
            voice_turn = (
                getattr(session, "barge_in_candidate_turn", None)
                if _barge_in_candidate_active(session)
                else _ensure_voice_turn(session_id, session)
            )
            voice_turn.vad_endpoint_count += 1
            if voice_turn.speech_ended_at <= 0:
                now = time.monotonic()
                last_speech = getattr(session.processor, "last_speech_monotonic", 0.0)
                voice_turn.speech_ended_at = (
                    last_speech if 0 < last_speech <= now else now
                )
            await _feed_streaming_stt(session, audio_bytes)
            latest_transcript = _sync_voice_turn_transcript(
                session_id,
                session,
                reason="vad_endpoint_candidate",
                voice_turn=voice_turn,
            )
            buffered_audio_ms = _pcm_duration_ms(session.processor.turn_buffer)
            stt_bytes_fed = session.stt_stream.bytes_fed if session.stt_stream is not None else 0
            perf.log(
                "vad_endpoint_candidate",
                session=session_id,
                turn_id=voice_turn.turn_id,
                source="user",
                scenario="voice",
                buffered_audio_ms=buffered_audio_ms,
                stt_bytes_fed_ms=_pcm_duration_ms(stt_bytes_fed),
                stt_audio_gap_ms=round(max(0.0, buffered_audio_ms - _pcm_duration_ms(stt_bytes_fed)), 1),
                latest_transcript_chars=len(latest_transcript),
                stt_stream_active=session.stt_stream is not None,
            )

            if _barge_in_candidate_active(session):
                decision = await _resolve_barge_in_candidate(
                    session_id, session, endpointed=True
                )
                # COMMIT already handed off inside resolve when still endpointed.
                if decision is not AdmissionAction.COMMIT:
                    return
            else:
                await _handoff_endpointed_voice_turn(session_id, session, voice_turn)

        elif session.stt_stream is not None and session.processor.turn_phase in {
            SpeechTurnPhase.SPEAKING,
            SpeechTurnPhase.ENDPOINT_CANDIDATE,
        }:
            await _feed_streaming_stt(session, audio_bytes)
            if _barge_in_candidate_active(session):
                # Soft-wait after VAD endpoint must keep endpointed=True so STT catch-up
                # can commit/suppress without waiting for max-wait or another silence edge.
                soft_endpointed = (
                    session.processor.turn_phase == SpeechTurnPhase.ENDPOINT_CANDIDATE
                )
                await _resolve_barge_in_candidate(
                    session_id, session, endpointed=soft_endpointed
                )
    
    except Exception as e:
        logger.error(f"Error in handle_audio_stream: {e}", exc_info=True)


# --- Wake Word Feedback Handler ---
@register_handler(WSMessageType.WAKEWORD_FEEDBACK)
async def handle_wakeword_feedback(session_id: str, message: WSMessage) -> None:
    """
    Save the last-detected wake word audio as a training sample.
    label: "true_positive" saves to feedback/positives/ when VOICE.wakeword_save_positive_feedback
    is enabled; "false_positive" always saves to feedback/negatives/.
    """
    try:
        label = message.data.get("label", "")
        if label not in ("true_positive", "false_positive"):
            logger.warning(f"Invalid wakeword feedback label: {label!r}")
            return

        if label == "true_positive" and not settings.VOICE.wakeword_save_positive_feedback:
            logger.debug("Skipping wakeword positive feedback save (disabled in config)")
            return

        session = manager.get_session(session_id)
        if not session:
            return

        ww = session.processor.wakeword_service
        if not ww:
            return

        pcm_bytes = ww.consume_detection_audio()
        if not pcm_bytes:
            # No audio buffered — stale or duplicate feedback, ignore silently
            return

        subfolder = "positives" if label == "true_positive" else "negatives"
        # Repo-only contributor path; packaged apps have no training/ tree.
        training_root = settings.BASE_DIR.parent / "training" / "wakeword"
        if not training_root.is_dir():
            logger.debug("Skipping wakeword feedback save (no training directory)")
            return
        feedback_dir = training_root / "data" / "feedback" / subfolder
        feedback_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        wav_path = feedback_dir / f"feedback_{timestamp}.wav"
        wav_bytes = _pcm_to_wav(pcm_bytes)
        wav_path.write_bytes(wav_bytes)

        logger.info(f"Saved wakeword {label} sample: {wav_path.name}")

    except Exception as e:
        logger.error(f"Error in handle_wakeword_feedback: {e}", exc_info=True)
