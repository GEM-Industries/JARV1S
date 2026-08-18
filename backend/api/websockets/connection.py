import logging
import time
import asyncio
from uuid import uuid4
from typing import Any, Dict, List, Set, Optional, TYPE_CHECKING
from fastapi import WebSocket
from dataclasses import dataclass, field

from .models import WSResponse
from .types import WSMessageType
from core.auth.device_models import DeviceLocation

from .presence import LocationRef, PresenceIdentity
from services.events import event_bus, Event, EventType
from core.config import settings
from core.attention.service import attention_service
from core.preferences.models import UserPreferences
from core.preferences.service import get_user_preferences
from core.plugins.widget_snapshots import collect_widget_snapshots
from core.voice.vad_service import TenVADService
from core.voice.wakeword_service import WakeWordService
from core.voice.processor import SpeechProcessor
from core.voice.speaker_verifier import EnrolledSpeakerVerifier, SpeakerEvidence
from core.voice.turn_admission import AdmissionSource

if TYPE_CHECKING:
    from core.voice.streaming_stt import StreamingSTTCoordinator
    from core.voice.turn_detector import AudioTurnDetectorSession
    from core.turns.delivery import VoiceDelivery

logger = logging.getLogger(__name__)

NODE_REPLACED_CLOSE_CODE = 4001
DEVICE_REVOKED_CLOSE_CODE = 4002


def attention_state_payload(state: Any) -> Dict[str, Any]:
    raw = state.model_dump() if hasattr(state, "model_dump") else dict(state or {})
    expires_at = raw.get("expires_at")
    updated_at = raw.get("updated_at")
    return {
        "owner_id": raw.get("owner_id", ""),
        "mode": raw.get("mode", "active"),
        "expires_at": expires_at.isoformat() if hasattr(expires_at, "isoformat") else expires_at,
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
    }


def session_state_payload(session: Any) -> Dict[str, Any]:
    return {
        "soft_muted": bool(getattr(session, "soft_muted", False)),
    }


def preferences_payload(preferences: UserPreferences) -> Dict[str, Any]:
    return preferences.model_dump(mode="json")


@dataclass
class VoiceInputTurn:
    """Voice input capture state shared by VAD, streaming STT, and late continuation."""
    turn_id: str
    last_endpoint_monotonic: float = 0.0
    transcript_text: str = ""
    continuation_prefix: str = ""
    endpoint_candidate_started_at: float = 0.0
    endpoint_candidate_text_chars: int = 0
    # True when this turn opened from WAKE_WORD_DETECTED (vocative), not mid-conversation VAD.
    from_wake: bool = False
    # EOU submit-latency visibility (monotonic clocks; survive detector continue_turn).
    speech_ended_at: float = 0.0
    first_transcript_at: float = 0.0
    continue_count: int = 0
    awaiting_stt_count: int = 0
    vad_endpoint_count: int = 0
    # Verified owner identity for barge-in commits only; not presence identity.
    speaker_id: Optional[str] = None
    speaker_confidence: Optional[float] = None
    speaker_source: Optional[str] = None
    # Pre-agent admission stamp (wake / barge_in / followup / push_to_talk).
    admission_source: AdmissionSource | None = None
    admission_reason: Optional[str] = None


@dataclass
class Session:
    """Unified session state."""
    websocket: WebSocket
    processor: SpeechProcessor
    presence: PresenceIdentity
    subscriptions: Set[str] = field(default_factory=set)
    connected_at: float = field(default_factory=time.monotonic)
    last_seen_at: float = field(default_factory=time.monotonic)
    # Last user-origin turn on this connection; not bumped by heartbeat/reconnect.
    last_active_at: float | None = None

    
    # --- Turn State (Persisted across chunks) ---
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Voice-only post-commit handoff: final STT, local command check, scheduling process_turn.
    # Active only during the narrow window between endpointing commit and assistant run start.
    # Fast recovery can cancel this to restart continuation; it must not be confused with an
    # active assistant run.  Cleared when _resolve_endpoint_candidate exits.
    accepted_input_task: Optional[asyncio.Task] = None
    # Active assistant/delivery execution: process_turn, trigger delivery, prefetch hit, protocol.
    # This is what barge-in and interruption cancel.  Not set during voice input capture.
    current_run_task: Optional[asyncio.Task] = None
    endpoint_decision_task: Optional[asyncio.Task] = None
    tts_sentence_queue: Optional[asyncio.Queue] = None
    stt_stream: Optional["StreamingSTTCoordinator"] = None
    turn_detector: Optional["AudioTurnDetectorSession"] = None
    voice_turn: Optional[VoiceInputTurn] = None
    pending_attachments: List[Dict[str, Any]] = field(default_factory=list)
    preferences: UserPreferences = field(default_factory=lambda: UserPreferences(owner_id=settings.DEFAULT_USER_ID))
    soft_muted: bool = False
    soft_mute_resume_task: Optional[asyncio.Task] = None

    # --- Delivery State ---
    first_audio_sent: bool = False           # True once TTS audio has actually reached the frontend (in-flight turn only; reset by aclose)
    last_turn_audio_sent: bool = False       # whether any audio reached the frontend in the most recent turn
    last_turn_audio_completed: bool = False  # whether that turn completed without a TTS failure
    last_turn_routed_tools: Set[str] = field(default_factory=set)
    active_audio_turn_id: Optional[str] = None             # turn_id for the latest TTS audio stream awaiting playback_end
    current_delivery: Optional["VoiceDelivery"] = None     # active delivery attempt; owns turn-scoped TTS cancellation
    current_trigger_instance_id: Optional[str] = None       # active announce trigger; explicit STOP dismisses it instead of retrying
    barge_in_candidate_started_at: float = 0.0             # pending speech-over-AI candidate start time; 0 means inactive
    barge_in_candidate_turn: Optional[VoiceInputTurn] = None # reversible candidate capture; promoted to voice_turn only on commit
    barge_in_candidate_task: Optional[asyncio.Task] = None  # max-wait resolver for a pending barge-in candidate
    barge_in_candidate_committed: bool = False             # candidate has been promoted to a real user turn
    barge_in_speaker_task: Optional[asyncio.Task] = None   # at-most-one speaker verify for the active candidate
    barge_in_speaker_evidence: Optional[SpeakerEvidence] = None
    barge_in_speaker_attempts: int = 0                     # at most two inferences per candidate
    # Shared enrolled-speaker verifier for wake Stage 2b and barge-in.
    speaker_verifier: Optional[EnrolledSpeakerVerifier] = None
    
    # --- Context (Dynamic, can be updated by client) ---
    # Initialization params (like timezone) are stored here on connect
    context: Dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        """Record that the client is actively talking to the backend."""
        self.last_seen_at = time.monotonic()

    def is_fresh(self, *, max_age_s: float) -> bool:
        return (time.monotonic() - self.last_seen_at) <= max_age_s

    @property
    def connection_id(self) -> str:
        return self.presence.connection_id

    @property
    def owner_id(self) -> str:
        return self.presence.owner_id

class ConnectionManager:
    """Manage WebSocket connections and session state."""
    
    def __init__(self):
        # Live sessions are keyed by transport connection_id. Owner/node lookup
        # maps keep existing user-targeted events working without making a socket
        # identity mean "the person currently speaking".
        self.sessions: Dict[str, Session] = {}
        self.default_connection_by_owner_id: Dict[str, str] = {}
        self.active_connection_by_node_key: Dict[str, str] = {}
        
        # Subscribe to UI events
        event_bus.subscribe(EventType.UI_UPDATE, self._handle_ui_update)
        event_bus.subscribe(EventType.UI_DELETE, self._handle_ui_delete)
        event_bus.subscribe(EventType.TASK_EVENT, self._handle_task_event)
        event_bus.subscribe(EventType.ATTENTION_CHANGED, self._handle_attention_changed)
        event_bus.subscribe(EventType.OPERATIONS_CHANGED, self._handle_operations_changed)
        event_bus.subscribe(EventType.ACTIVITY_CHANGED, self._handle_activity_changed)
        event_bus.subscribe(EventType.AUTH_OAUTH_CHANGED, self._handle_auth_oauth_changed)
        
    async def _handle_ui_update(self, event: Event) -> None:
        """Handle UI update events from the bus and push to WebSockets."""
        target_id = event.data.get("target_id") or event.data.get("session_id")
        envelope = event.data.get("envelope")
        
        if target_id and envelope:
            await self.send_message(
                target_id,
                WSResponse(
                    message_id="event",
                    type=WSMessageType.UI_UPDATE,
                    data=envelope
                )
            )
            
    async def _handle_ui_delete(self, event: Event) -> None:
        """Handle UI delete events from the bus and push to WebSockets."""
        target_id = event.data.get("target_id") or event.data.get("session_id")
        widget_id = event.data.get("widget_id")
        
        if target_id and widget_id:
            await self.send_message(
                target_id,
                WSResponse(
                    message_id="event",
                    type=WSMessageType.UI_DELETE,
                    data={"widget_id": widget_id}
                )
            )

    async def _handle_task_event(self, event: Event) -> None:
        """Forward real-time agent task events to the connected WebSocket client."""
        target_id = event.data.get("target_id") or event.data.get("session_id")
        payload = event.data.get("payload")
        if target_id and payload:
            await self.send_message(
                target_id,
                WSResponse(
                    message_id="event",
                    type=WSMessageType.TASK_UPDATE,
                    data=payload,
                ),
            )

    async def _handle_operations_changed(self, event: Event) -> None:
        """Forward operations invalidations to every connected node for the owner."""
        owner_id = event.data.get("owner_id")
        scope = event.data.get("scope")
        if not owner_id or not scope:
            return

        for session in list(self.sessions.values()):
            if session.owner_id != owner_id:
                continue
            await self.send_message(
                session.connection_id,
                WSResponse(
                    message_id="event",
                    type=WSMessageType.OPERATIONS_CHANGED,
                    data={"scope": scope},
                ),
            )

    async def _handle_activity_changed(self, event: Event) -> None:
        """Forward activity invalidations to every connected node for the owner."""
        owner_id = event.data.get("owner_id")
        if not owner_id:
            return

        for session in list(self.sessions.values()):
            if session.owner_id != owner_id:
                continue
            await self.send_message(
                session.connection_id,
                WSResponse(
                    message_id="event",
                    type=WSMessageType.ACTIVITY_CHANGED,
                ),
            )

    async def _handle_auth_oauth_changed(self, event: Event) -> None:
        """Forward OAuth completion to connected clients for the owner."""
        owner_id = event.data.get("owner_id")
        if not owner_id:
            return

        payload = {
            "app": event.data.get("app"),
            "success": event.data.get("success"),
            "loaded": event.data.get("loaded"),
            "kind": event.data.get("kind"),
        }
        for session in list(self.sessions.values()):
            if session.owner_id != owner_id:
                continue
            await self.send_message(
                session.connection_id,
                WSResponse(
                    message_id="event",
                    type=WSMessageType.AUTH_OAUTH_CHANGED,
                    data=payload,
                ),
            )

    async def _handle_attention_changed(self, event: Event) -> None:
        """Forward owner attention changes to every connected node."""
        owner_id = event.data.get("owner_id")
        if not owner_id:
            return

        data = {"attention": attention_state_payload(event.data.get("state"))}
        for session in list(self.sessions.values()):
            if session.owner_id != owner_id:
                continue
            await self.send_message(
                session.connection_id,
                WSResponse(
                    message_id="event",
                    type=WSMessageType.STATUS,
                    data=data,
                ),
            )

    async def broadcast_preferences_update(self, preferences: UserPreferences) -> None:
        """Update live owner sessions and notify connected nodes."""
        data = {"preferences": preferences_payload(preferences)}
        for session in list(self.sessions.values()):
            if session.owner_id != preferences.owner_id:
                continue
            session.preferences = preferences
            await self.send_message(
                session.connection_id,
                WSResponse(
                    message_id="event",
                    type=WSMessageType.PREFERENCES_UPDATED,
                    data=data,
                ),
            )

    async def broadcast_presence_changed(self, owner_id: str) -> None:
        """Notify an owner's connected clients to refresh their presence view."""
        for session in list(self.sessions.values()):
            if session.owner_id != owner_id:
                continue
            await self.send_message(
                session.connection_id,
                WSResponse(
                    message_id="event",
                    type=WSMessageType.PRESENCE_CHANGED,
                ),
            )

    def resolve_connection_id(self, target_id: str) -> Optional[str]:
        """Resolve a connection, owner, or node key to the active connection id.

        User-origin delivery must use ``get_session_by_connection`` instead.
        This resolver is for proactive/default routing and legacy call sites only.
        """
        if target_id in self.sessions:
            return target_id
        if target_id in self.default_connection_by_owner_id:
            return self.default_connection_by_owner_id[target_id]
        if target_id in self.active_connection_by_node_key:
            return self.active_connection_by_node_key[target_id]
        return None

    def get_session_by_connection(self, connection_id: str) -> Optional[Session]:
        """Return the live session for an exact transport connection id."""
        return self.sessions.get(connection_id)

    def get_default_session_for_owner(self, owner_id: str) -> Optional[Session]:
        """Return the owner's last-connected session for proactive/default delivery."""
        connection_id = self.default_connection_by_owner_id.get(owner_id)
        return self.sessions.get(connection_id) if connection_id else None

    def list_owner_sessions(self, owner_id: str) -> list[Session]:
        return [session for session in self.sessions.values() if session.owner_id == owner_id]

    def record_user_turn_activity(self, connection_id: str) -> None:
        session = self.sessions.get(connection_id)
        if session is not None:
            session.last_active_at = time.time()

    def list_live_endpoints(self, owner_id: str):
        from core.triggers.endpoint_router import LiveEndpoint

        return [
            LiveEndpoint(
                connection_id=session.connection_id,
                node_id=session.presence.node_id,
                capabilities=session.presence.capabilities,
                location=session.presence.location,
                last_active_at=session.last_active_at,
                connected_at=session.connected_at,
            )
            for session in self.list_owner_sessions(owner_id)
        ]

    def update_node_location(self, owner_id: str, node_id: str, location: DeviceLocation) -> bool:
        """Refresh live presence for connected sessions on this node (best-effort)."""
        loc = LocationRef(
            provider=location.provider,
            room_id=location.room_id,
            room_name=location.room_name,
            ha_area_id=location.ha_area_id,
            ha_device_id=location.ha_device_id,
            ha_entity_id=location.ha_entity_id,
        )
        updated = False
        for session in self.list_owner_sessions(owner_id):
            if session.presence.node_id != node_id:
                continue
            old = session.presence
            session.presence = PresenceIdentity(
                connection_id=old.connection_id,
                owner_id=old.owner_id,
                node_id=old.node_id,
                node_label=old.node_label,
                capabilities=old.capabilities,
                device_kind=old.device_kind,
                location=loc,
            )
            session.context["location_ref"] = loc.model_dump()
            updated = True
        return updated

    def get_owner_id(self, target_id: str) -> str:
        session = self.get_session(target_id)
        return session.owner_id if session else target_id

    def get_presence_snapshot(self) -> list[dict[str, Any]]:
        """Return lightweight live-node state for diagnostics."""
        now = time.monotonic()
        return [
            {
                **session.presence.model_dump(),
                "connected_for_s": round(now - session.connected_at, 1),
                "last_seen_age_s": round(now - session.last_seen_at, 1),
            }
            for session in self.sessions.values()
        ]

    async def connect(self, websocket: WebSocket, presence: PresenceIdentity, timezone: str = "UTC") -> str:
        """Accept a new WebSocket connection."""
        await websocket.accept()

        existing_connection_id = self.active_connection_by_node_key.get(presence.node_key)
        logger.info(
            "WebSocket connect requested: connection=%s owner=%s node=%s kind=%s existing_connection=%s active_sessions=%s",
            presence.connection_id,
            presence.owner_id,
            presence.node_id,
            presence.device_kind,
            existing_connection_id,
            len(self.sessions),
        )
        if existing_connection_id:
            logger.info(
                "Replacing existing WebSocket node: owner=%s node=%s old_connection=%s new_connection=%s",
                presence.owner_id,
                presence.node_id,
                existing_connection_id,
                presence.connection_id,
            )
            await self.disconnect(existing_connection_id, code=NODE_REPLACED_CLOSE_CODE, reason="node_replaced")
        
        # Instantiate services per session.
        # Note: These MUST be per-session because they maintain internal state/buffers
        # for their respective audio streams.
        vad = TenVADService(threshold=settings.VOICE.vad_threshold)
        speaker_verifier = EnrolledSpeakerVerifier(owner_id=presence.owner_id)
        wakeword = WakeWordService(
            owner_id=presence.owner_id,
            speaker_verifier=speaker_verifier,
        )

        context = {"timezone": timezone, **presence.context()}
        preferences = await get_user_preferences(presence.owner_id)
        self.sessions[presence.connection_id] = Session(
            websocket=websocket,
            processor=SpeechProcessor(vad_service=vad, wakeword_service=wakeword),
            presence=presence,
            context=context,
            preferences=preferences,
            speaker_verifier=speaker_verifier,
        )
        self.default_connection_by_owner_id[presence.owner_id] = presence.connection_id
        self.active_connection_by_node_key[presence.node_key] = presence.connection_id
        attention_state = await attention_service.get_state(presence.owner_id)
        
        await self.send_message(
            presence.connection_id,
            WSResponse(
                message_id="system",
                type=WSMessageType.CONNECT,
                data={
                    "status": "connected",
                    **presence.model_dump(),
                    "attention": attention_state_payload(attention_state),
                    "session": session_state_payload(self.sessions[presence.connection_id]),
                    "preferences": preferences_payload(preferences),
                },
            )
        )

        widget_snapshots = await collect_widget_snapshots(presence.owner_id)
        await self.send_message(
            presence.connection_id,
            WSResponse(
                message_id="system",
                type=WSMessageType.UI_SNAPSHOT,
                data={
                    "widgets": [
                        envelope.model_dump(mode="json")
                        for envelope in widget_snapshots
                    ],
                },
            )
        )

        if (
            presence.connection_id not in self.sessions
            or self.active_connection_by_node_key.get(presence.node_key) != presence.connection_id
        ):
            logger.debug(
                "Skipping connected publish for replaced WebSocket: connection=%s node=%s",
                presence.connection_id,
                presence.node_id,
            )
            return presence.connection_id
        
        logger.info(
            "New WebSocket connection: connection=%s owner=%s node=%s kind=%s room=%s",
            presence.connection_id,
            presence.owner_id,
            presence.node_id,
            presence.device_kind,
            presence.location.room_name or presence.location.room_id,
        )

        # Notify system that session is connected (for pending alerts, etc.)
        await event_bus.publish(
            Event(
                type=EventType.SESSION_CONNECTED,
                source="connection_manager",
                data={
                    "owner_id": presence.owner_id,
                    "session_id": presence.owner_id,
                    "connection_id": presence.connection_id,
                    "node_id": presence.node_id,
                    "location": presence.location.model_dump(),
                },
            )
        )
        await self.broadcast_presence_changed(presence.owner_id)
        return presence.connection_id
    
    async def disconnect(
        self,
        target_id: str,
        websocket: Optional[WebSocket] = None,
        *,
        code: int = 1000,
        reason: str = "",
    ) -> None:
        """Handle WebSocket disconnection."""
        connection_id = self.resolve_connection_id(target_id)
        logger.info(
            "WebSocket disconnect requested: requested=%s resolved=%s has_websocket=%s",
            target_id,
            connection_id,
            websocket is not None,
        )
        if connection_id:
            session = self.sessions.get(connection_id)
            if not session:
                return
            accepted_input_active = bool(session.accepted_input_task and not session.accepted_input_task.done())
            current_run_active = bool(session.current_run_task and not session.current_run_task.done())
            endpoint_active = bool(session.endpoint_decision_task and not session.endpoint_decision_task.done())
            stt_active = session.stt_stream is not None
            tts_queue_depth = (
                session.tts_sentence_queue.qsize()
                if session.tts_sentence_queue is not None
                else None
            )
            logger.debug(
                "WebSocket disconnect session state | connection=%s owner=%s node=%s "
                "code=%s reason=%r accepted_input_active=%s current_run_active=%s "
                "endpoint_active=%s stt_active=%s "
                "current_delivery_response_id=%s first_audio_sent=%s last_turn_audio_sent=%s "
                "tts_queue_depth=%s",
                connection_id,
                session.owner_id,
                session.presence.node_id,
                code,
                reason,
                accepted_input_active,
                current_run_active,
                endpoint_active,
                stt_active,
                getattr(session.current_delivery, "response_id", None),
                session.first_audio_sent,
                session.last_turn_audio_sent,
                tts_queue_depth,
            )
            
            # Safe disconnect: only remove if it's the same websocket object
            # This prevents a reconnecting client from accidentally deleting its own new session
            if websocket and session.websocket != websocket:
                logger.debug(f"Ignoring disconnect for replaced session: {connection_id}")
                return

            try:
                if session.accepted_input_task is not None and not session.accepted_input_task.done():
                    session.accepted_input_task.cancel("disconnect")
                if session.current_run_task is not None and not session.current_run_task.done():
                    session.current_run_task.cancel("disconnect")
                if session.endpoint_decision_task is not None:
                    session.endpoint_decision_task.cancel("disconnect")
                if session.soft_mute_resume_task is not None and not session.soft_mute_resume_task.done():
                    session.soft_mute_resume_task.cancel("disconnect")
                if session.stt_stream is not None:
                    await session.stt_stream.close(reason="disconnect")
                if session.turn_detector is not None:
                    await session.turn_detector.aclose()
                    session.turn_detector = None
                await session.websocket.close(code=code, reason=reason)
            except Exception:
                pass # Already closed
            
            self.sessions.pop(connection_id, None)
            if self.default_connection_by_owner_id.get(session.owner_id) == connection_id:
                self.default_connection_by_owner_id.pop(session.owner_id, None)
            if self.active_connection_by_node_key.get(session.presence.node_key) == connection_id:
                self.active_connection_by_node_key.pop(session.presence.node_key, None)

            from core.tool_router import tool_router
            tool_router.clear_session(connection_id)

            logger.info(
                "WebSocket disconnected: connection=%s owner=%s node=%s",
                connection_id,
                session.owner_id,
                session.presence.node_id,
            )
            await self.broadcast_presence_changed(session.owner_id)
    
    def get_session(self, target_id: str) -> Optional[Session]:
        """Get the live session addressed by connection, owner, or node id."""
        connection_id = self.resolve_connection_id(target_id)
        return self.sessions.get(connection_id) if connection_id else None
    
    async def send_message(self, target_id: str, message: WSResponse) -> None:
        """Send a message to a connection, owner default, or active node."""
        connection_id = self.resolve_connection_id(target_id)
        if not connection_id or connection_id not in self.sessions:
            logger.warning(f"Attempted to send message to unknown target: {target_id}")
            return
        
        try:
            await self.sessions[connection_id].websocket.send_text(
                message.model_dump_json()
            )
        except Exception as e:
            logger.error(f"Error sending message to {target_id}: {e}")
            await self.disconnect(connection_id)

    async def send_voice_response(
        self, target_id: str, message_type: WSMessageType, data: Dict[str, Any], *, message_id: Optional[str] = None,
    ) -> str:
        """Helper to send a wrapped voice event response. Returns the message_id."""
        msg_id = message_id or str(uuid4())
        await self.send_message(
            target_id,
            WSResponse(
                message_id=msg_id,
                type=message_type,
                data=data
            )
        )
        return msg_id
    
    async def broadcast(self, message: WSResponse, exclude: Optional[str] = None) -> None:
        """Broadcast a message to all connected clients."""
        disconnected = []
        
        for session_id, session in self.sessions.items():
            if session_id != exclude:
                try:
                    await session.websocket.send_text(message.model_dump_json())
                except Exception as e:
                    logger.error(f"Error broadcasting to {session_id}: {e}")
                    disconnected.append(session_id)
        
        for session_id in disconnected:
            await self.disconnect(session_id)
    
    async def handle_subscription(self, session_id: str, event_type: str) -> None:
        """Handle event subscription."""
        session = self.get_session(session_id)
        if session:
            session.subscriptions.add(event_type)
            logger.debug(f"Session {session_id} subscribed to {event_type}")
    
    async def handle_unsubscription(self, session_id: str, event_type: str) -> None:
        """Handle event unsubscription."""
        session = self.get_session(session_id)
        if session:
            session.subscriptions.discard(event_type)
            logger.debug(f"Session {session_id} unsubscribed from {event_type}")
    
    def get_subscriptions(self, session_id: str) -> Set[str]:
        """Get all subscriptions for a session."""
        session = self.get_session(session_id)
        if session:
            return session.subscriptions
        return set()

manager = ConnectionManager() 