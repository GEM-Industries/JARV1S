from enum import Enum


class WSMessageType(str, Enum):
    """
    WebSocket message types defining the communication protocol.
    
    Directions:
    - [C->S]: Client to Server
    - [S->C]: Server to Client
    - [<->]: Bidirectional
    """
    
    # --- System Messages ---
    CONNECT = "system.connect"       # [S->C] Initial connection ack
    DISCONNECT = "system.disconnect" # [<->] Connection termination
    ERROR = "system.error"           # [S->C] Critical error notification
    PING = "system.ping"             # [C->S] Keepalive
    PONG = "system.pong"             # [S->C] Keepalive response
    CLEAR_TRANSCRIPT = "system.clear_transcript"  # [S->C] Wipe frontend conversation transcript
    
    # --- User Input ---
    USER_AUDIO = "user_audio"              # [C->S] Raw PCM audio chunk (base64)
    USER_TEXT = "user_text"                # [C->S] Text input from client
    USER_ATTACHMENT = "user_attachment"    # [C->S] Image/file attachment (base64)

    # --- Audio/Voice State ---
    JARVIS_AUDIO = "jarvis_audio"          # [S->C] TTS audio chunk (base64, turn_id)
    AUDIO_CUE = "audio.cue"                # [S->C] Short local UI/audio cue {phase}
    TTS_END = "audio.tts_end"              # [S->C] Backend finished producing TTS for the delivery stream {turn_id}
    SPEECH_START = "speech.start"          # [S->C] VAD detected speech start (UI trigger)
    SPEECH_END = "speech.end"              # [S->C] VAD detected silence/turn end
    PLAYBACK_END = "audio.playback_end"    # [C->S] Client finished playing TTS buffer {turn_id}
    MUTE = "audio.mute"                    # [C->S] Client mic muted — reset processor to PASSIVE
    VOICE_ACTIVATE = "voice.activate"      # [C->S] Open active listening window
    VOICE_COMMIT = "voice.commit"          # [C->S] Push-to-talk released — submit captured turn
    STOP = "system.stop"                   # [C->S] Interrupt current generation/playback
    CONTEXT_UPDATE = "context.update"      # [C->S] Client state update (location, timezone)
    
    # --- Conversation Items ---
    TRANSCRIPT = "conversation.transcript"         # [S->C] Final user transcript
    PARTIAL_TRANSCRIPT = "conversation.partial"    # [S->C] Live user transcript updates
    RESPONSE = "conversation.response"             # [S->C] Assistant text response (partial/final)
    NO_REPLY = "conversation.no_reply"             # [S->C] Assistant intentionally stayed silent
    RETRACT = "conversation.retract"               # [S->C] Remove a streamed item by response_id / message_id
    CODE = "conversation.code"                     # [S->C] Capability call receipt
    CODE_OUTPUT = "conversation.code_output"       # [S->C] Capability result
    REASONING = "conversation.reasoning"           # [S->C] Provider reasoning (text clients)
    
    # --- Status & Events ---
    STATUS = "status.update"                  # [S->C] High-level state (idle, listening, thinking, speaking)
    CONTEXT_METRICS = "context.metrics"       # [S->C] Context budget usage after each turn
    OPERATIONS_CHANGED = "operations.changed" # [S->C] Operations definitions changed {scope}
    ACTIVITY_CHANGED = "activity.changed"     # [S->C] Activity feed changed
    PRESENCE_CHANGED = "presence.changed"     # [S->C] Owner device presence changed
    PREFERENCES_UPDATED = "preferences.update" # [S->C] Owner runtime preferences changed
    EVENT_SUBSCRIBE = "event.subscribe"       # [C->S] Request to subscribe to backend events
    EVENT_UNSUBSCRIBE = "event.unsubscribe"   # [C->S] Request to unsubscribe

    # --- Notifications ---
    NOTIFICATION_SOUND = "notification.sound"  # [S->C] Play a notification chime/alarm

    # --- UI & Widgets ---
    UI_ACTION = "ui.action"                   # [C->S] User interaction with a widget
    UI_PIN = "ui.pin"                         # [C->S] Persist or clear a pinned widget
    UI_UPDATE = "ui.update"                   # [S->C] Push new widget data
    UI_SNAPSHOT = "ui.snapshot"               # [S->C] Bulk restore active widgets
    UI_DELETE = "ui.delete"                   # [S->C] Remove widget

    # --- Auth ---
    AUTH_OAUTH_CHANGED = "auth.oauth.changed" # [S->C] OAuth flow completed {app, success, loaded, kind}

    # --- Background Tasks ---
    TASK_UPDATE = "task.update"               # [S->C] Real-time agent progress event

    # --- Wake Word Feedback ---
    WAKEWORD_FEEDBACK = "wakeword.feedback"   # [C->S] User feedback on detection {label: "true_positive"|"false_positive"}

    # --- Client Diagnostics ---
    CLIENT_DIAGNOSTICS = "client.diagnostics"  # [C->S] Bounded connection/audio incident breadcrumbs


    @classmethod
    def get_category(cls, message_type: str) -> str:
        """Get the category of a message type."""
        return message_type.split('.')[0]
