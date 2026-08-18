from enum import Enum, auto

class EventCategory(str, Enum):
    """Categories for event types."""
    SYSTEM = "system"
    VOICE = "voice"
    CONVERSATION = "conversation"
    FUNCTION = "function"
    PLUGIN = "plugin"  # For future plugin system
    DEVICE = "device"  # For smart home integration
    USER = "user"     # For user-related events
    STATE = "state"   # For state changes
    TRIGGER = "trigger"  # For trigger lifecycle events
    ATTENTION = "attention"  # For attention/DND state changes

class EventType(str, Enum):
    """All event types in the system."""
    
    # --- System Events ---
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_ERROR = "system.error"
    SYSTEM_WARNING = "system.warning"
    SYSTEM_INFO = "system.info"
    SESSION_CONNECTED = "system.session.connected"
    ATTENTION_CHANGED = "attention.changed"
    
    # --- Voice Lifecycle ---
    # 1. Wake (Passive -> Active)
    VOICE_WAKE = "voice.wake"                          # data: {session_id}
    
    # 2. User Speech
    VOICE_USER_START = "voice.user.start"              # data: {session_id}
    VOICE_INTERRUPT = "voice.interrupt"                # data: {session_id} (Manual stop or barge-in)
    VOICE_USER_END = "voice.user.end"                  # data: {session_id} (VAD detected silence)
    
    # 3. System Response
    VOICE_TURN_END = "voice.turn.end"                  # data: {session_id, transcript}
    
    # 4. State
    VOICE_TIMEOUT = "voice.timeout"                    # data: {session_id}
    VOICE_ERROR = "voice.error"                        # data: {session_id, error}
    VOICE_SESSION_END = "voice.session.end"            # data: {session_id} (e.g. stop_listening)

    # --- Conversation State ---
    CONVERSATION_STARTED = "conversation.started"
    CONVERSATION_ENDED = "conversation.ended"
    CONVERSATION_CONTEXT_UPDATED = "conversation.context.updated"
    
    # --- Tool/Function Execution ---
    FUNCTION_CALLED = "function.called"
    FUNCTION_COMPLETED = "function.completed"
    FUNCTION_FAILED = "function.failed"
    
    # --- Protocols ---
    PROTOCOL_RUN = "plugin.protocol.run"              # data: {owner_id, protocol_name}

    # --- Operations Definitions ---
    OPERATIONS_CHANGED = "operations.changed"         # data: {owner_id, scope}

    # --- Activity Feed ---
    ACTIVITY_CHANGED = "activity.changed"             # data: {owner_id}

    # --- UI & Widgets ---
    UI_UPDATE = "ui.update"                   # Push new/updated widget to client
    UI_DELETE = "ui.delete"                   # Remove widget from client

    # --- Auth / Integrations ---
    AUTH_OAUTH_CHANGED = "auth.oauth.changed"  # OAuth connect/reconnect completed

    # --- Background Tasks ---
    TASK_EVENT = "task.event"                 # Real-time agent progress (tool_start/end, text)

    # --- Triggers ---
    TRIGGER_DUE = "trigger.due"                      # data: {instance_id, owner_id}
    TRIGGER_ACKED = "trigger.acked"                  # data: {instance_id, owner_id}
    TRIGGER_RETRY_AWAITING = "trigger.retry_awaiting"  # data: {owner_id, session_id?}

    @classmethod
    def get_category(cls, event_type: str) -> EventCategory:
        """Get the category of an event type."""
        return EventCategory(event_type.split('.')[0]) 