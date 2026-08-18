/**
 * WebSocket message types matching backend WSMessageType enum.
 */
export type WSMessageType =
  // System Messages
  | 'system.connect'
  | 'system.disconnect'
  | 'system.error'
  | 'system.ping'
  | 'system.pong'
  | 'system.stop'
  | 'system.clear_transcript'
  // Audio/Voice State
  | 'user_audio'
  | 'user_text'
  | 'user_attachment'
  | 'jarvis_audio'
  | 'audio.cue'
  | 'audio.tts_end'
  | 'speech.start'
  | 'speech.end'
  | 'audio.playback_end'
  | 'audio.mute'
  | 'voice.activate'
  | 'voice.commit'
  | 'context.update'
  // Conversation Items
  | 'conversation.transcript'
  | 'conversation.partial'
  | 'conversation.response'
  | 'conversation.no_reply'
  | 'conversation.retract'
  | 'conversation.code'
  | 'conversation.code_output'
  | 'conversation.reasoning'
  // Status & Events
  | 'status.update'
  | 'context.metrics'
  | 'operations.changed'
  | 'activity.changed'
  | 'presence.changed'
  | 'preferences.update'
  | 'auth.oauth.changed'
  | 'event.subscribe'
  | 'event.unsubscribe'
  // Notifications
  | 'notification.sound'
  // UI & Widgets
  | 'ui.action'
  | 'ui.pin'
  | 'ui.update'
  | 'ui.snapshot'
  | 'ui.delete'
  // Background Tasks
  | 'task.update'
  // Wake Word Feedback
  | 'wakeword.feedback'
  // Client diagnostics (bounded breadcrumbs)
  | 'client.diagnostics'

export type WidgetSize =
  | 'mini'
  | 'small'
  | 'wide'
  | 'tall'
  | 'large'
  | 'large-wide'
  | 'hero'
  | 'full-width'

export interface PresenceLocationRef {
  provider: 'manual' | 'home_assistant' | 'unknown'
  room_id: string | null
  room_name: string | null
  ha_area_id: string | null
  ha_device_id: string | null
  ha_entity_id: string | null
}

export interface PresenceIdentity {
  connection_id: string
  owner_id: string
  node_id: string
  node_label?: string | null
  capabilities: string[]
  location: PresenceLocationRef
}

export type AttentionMode = 'active' | 'quiet' | 'paused'

export interface AttentionState {
  owner_id: string
  mode: AttentionMode
  expires_at: string | null
  updated_at: string | null
}

export interface JarvisSessionState {
  soft_muted: boolean
}

export interface JarvisPreferences {
  owner_id: string
  audio: {
    tool_cues_enabled: boolean
  }
}

export type AudioDeviceKind = 'audioinput' | 'audiooutput'

export type AudioProcessingProfile = 'standard' | 'call_compatibility'

export interface AudioDeviceOption {
  deviceId: string
  label: string
  kind: AudioDeviceKind
  isDefault: boolean
}

export interface AudioDeviceState {
  inputs: AudioDeviceOption[]
  outputs: AudioDeviceOption[]
  selectedInputId: string
  selectedOutputId: string
  activeInputLabel: string
  activeOutputLabel: string
  outputSelectionSupported: boolean
  permissionGranted: boolean
  processingProfile: AudioProcessingProfile
  appliedEchoCancellation: boolean | null
  automaticCallCompatibility: boolean
  activeCallApp: string | null
  /** Mic claimed but PCM not flowing (WebView capture stall). */
  captureStalled: boolean
  error: string | null
}

export interface WidgetLayout {
  size: WidgetSize;
  priority: number;
  group?: string;
}

export interface WSMessage {
  id: string
  type: WSMessageType
  data?: Record<string, unknown>
  error?: string
}

export interface WSResponse {
  message_id: string
  type: WSMessageType
  data?: Record<string, unknown>
  error?: string
}

export interface UIEnvelope {
  widget_id: string
  component: string
  data: Record<string, unknown>
  layout: WidgetLayout
  title?: string
  expires_at?: number
  created_at?: number
  pinned?: boolean
}

export interface TranscriptItem {
  id: string
  response_id?: string     // Groups streaming chunks from same response
  turn_id?: string         // Groups all assistant text segments from one turn
  text?: string
  code?: string            // For tool execution
  codeResult?: string      // Result of tool execution
  toolCallId?: string      // To link code and result
  type: 'text' | 'code' | 'notice' | 'reasoning'    // Discriminate between text, tool execution, notices, and reasoning
  status?: 'running' | 'completed' | 'error'
  sender: 'user' | 'assistant' | 'system'
  isPartial?: boolean
  /** Soft-mute STT preview; cleared when session leaves soft_muted. */
  ephemeral?: boolean
  isCollapsed?: boolean
  timestamp: number
  attachments?: Array<{ type: string; url: string }>
}

export type ConnectionState = 
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'
  | 'error'

export type AgentState = 
  | 'idle'
  | 'waking'
  | 'listening'
  | 'transcribing'
  | 'thinking'
  | 'composing_tool'
  | 'running_tool'
  | 'speaking'

// ---------------------------------------------------------------------------
// Integrations
// ---------------------------------------------------------------------------

export type IntegrationStatus = 'available' | 'connected' | 'error'
export type IntegrationKind = 'built_in' | 'composio'
export type IntegrationConnection = 'connected' | 'disconnected' | 'unknown'
export type IntegrationHealth = 'healthy' | 'degraded' | 'unavailable' | 'unknown'

export interface IntegrationSummary {
  name: string
  display_name: string
  connected: boolean
  loaded: boolean
  tool_count: number
  status: IntegrationStatus
  last_error?: string | null
  kind: IntegrationKind
  enabled: boolean
  /** "composio" | "<oauth-provider>" | null — which reconnect/disconnect flow to use */
  auth_type?: string | null
  /** OAuth providers that can authorize this integration, e.g. Calendar supports Google and Microsoft. */
  auth_providers?: string[]
  description: string
  connection: IntegrationConnection
  health: IntegrationHealth
  account?: string | null
  provider?: string | null
  capabilities: string[]
  last_used_at?: string | null
}

export interface OAuthCallbackMessage {
  type: 'jarvis:oauth_callback'
  success: boolean
  app: string
  loaded?: boolean
  kind?: 'bespoke' | 'composio'
}
