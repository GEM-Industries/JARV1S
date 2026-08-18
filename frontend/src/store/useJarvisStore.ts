import { create } from 'zustand'
import type { SetupState } from '../client/setupApi'
import { ConnectionState, AgentState, UIEnvelope, TranscriptItem, AudioDeviceState, type AttentionState, type JarvisPreferences, type JarvisSessionState } from '../types'

export interface PendingAttachment {
  dataUrl: string
  base64: string
  mimeType: string
}

export type OperationsScope = 'automations' | 'protocols' | 'schedules'

export type OperationsVersion = Record<OperationsScope, number>
export type HostState = 'unknown' | 'online' | 'degraded' | 'offline'

/** Ephemeral live-stage scratch — never hydrated from transcript history. */
export type LiveAssistantPreview = {
  text: string
  key: string
  turnId?: string
}

export type OperationsRunsFilter = {
  runKind: 'all' | 'headless' | 'task' | 'trigger' | 'automation' | 'user'
  nodeId?: string | null
  nodeLabel?: string | null
}

export interface ContextMetrics {
  tokens_used: number
  budget: number
  messages_kept: number
  messages_dropped: number
}

export interface TurnStageMetric {
  key: string
  label: string
  detail: string
  ms: number
  group: 'pre_response' | 'post_first_audio'
  iteration?: number
  stream_id?: string
  status?: string
  attempt?: number
  retry_count?: number
  timeout_ms?: number
}

export interface TurnDiagnostics {
  turn_id?: string
  source?: 'user' | 'system'
  modality?: 'voice' | 'text' | 'system'
  delivery?: 'announce' | 'silent' | 'on_exception' | 'suppressed' | 'prefetched' | null
  origin?: { type?: string; id?: string; name?: string; protocol_name?: string } | null
  status?: 'running' | 'completed' | 'cancelled' | 'handoff'
  response_ms: number | null
  total_ms: number | null
  stages?: TurnStageMetric[]
  active_stage?: TurnStageMetric | null
  tool_routing?: {
    policy_name?: string
    match_mode?: string
    matched_plugins?: string[]
    routed_tool_count?: number
    schema_tokens?: number
    route_latency_ms?: number
    used_routing_hint?: boolean
    used_session_carryover?: boolean
  }
  model: string | null
}

export interface Diagnostics {
  cpu_percent: number
  memory_mb: number
  loop_lag_ms: number
  thread_count: number
  uptime_s: number
  voice: {
    mode: string  // "passive" | "active_idle" | "active_ai_turn"
    wakeword_inferences_sec: number
    wakeword_save_positive_feedback?: boolean
    wakeword_feedback: { positive_count: number; negative_count: number } | null
  }
  turn: TurnDiagnostics
}

const defaultAudioDeviceState: AudioDeviceState = {
  inputs: [],
  outputs: [],
  selectedInputId: '',
  selectedOutputId: '',
  activeInputLabel: 'Microphone · Default',
  activeOutputLabel: 'Speaker · Default',
  outputSelectionSupported: false,
  permissionGranted: false,
  processingProfile: 'standard',
  appliedEchoCancellation: null,
  automaticCallCompatibility: false,
  activeCallApp: null,
  captureStalled: false,
  error: null,
}

const defaultPreferences: JarvisPreferences = {
  owner_id: '',
  audio: {
    tool_cues_enabled: true,
  },
}

interface JarvisState {
  connectionState: ConnectionState
  hostState: HostState
  agentState: AgentState
  isSpeaking: boolean
  isMuted: boolean
  isAudioContextReady: boolean
  transcript: TranscriptItem[]
  partialTranscript: string | null
  liveAssistantPreview: LiveAssistantPreview | null
  reconnectAttempt: number
  systemLatency: number | null
  coreName: string
  contextMetrics: ContextMetrics | null
  diagnostics: Diagnostics | null
  pendingAttachment: PendingAttachment | null
  audioDevices: AudioDeviceState
  attentionState: AttentionState | null
  sessionState: JarvisSessionState
  preferences: JarvisPreferences
  
  // Widget System
  widgets: Record<string, UIEnvelope>
  activeWidgetId: string | null
  isTranscriptVisible: boolean
  operationsVersion: OperationsVersion
  runsVersion: number
  presenceVersion: number

  // Shell overlays
  activeOverlay:
    | 'integrations'
    | 'operations'
    | 'smart_home'
    | 'home_assistant'
    | 'presence'
    | 'settings'
    | null
  operationsRunsFilter: OperationsRunsFilter | null
  settingsInitialSection: 'audio' | 'model' | 'credentials' | 'host' | null

  // Wake word feedback
  wakewordFeedbackVisible: boolean
  devicePairingRequired: boolean

  // Jarvis Host setup
  setupState: SetupState | null
  setupLoading: boolean
  setupRequired: boolean

  // Actions
  setConnectionState: (state: ConnectionState) => void
  setHostState: (state: HostState) => void
  setAgentState: (state: AgentState) => void
  setIsSpeaking: (isSpeaking: boolean) => void
  setIsMuted: (isMuted: boolean) => void
  setIsAudioContextReady: (isReady: boolean) => void
  addTranscriptItem: (item: TranscriptItem) => void
  updateOrAddTranscriptItem: (item: TranscriptItem) => void
  /** Live STT preview: either keyed in the transcript or floating while no id is known. */
  setUserTranscriptPreview: (params: {
    messageId?: string
    text: string
    ephemeral?: boolean
  }) => void
  /** Final user utterance: commit by the backend-provided transcript id. */
  commitUserTranscript: (item: {
    id: string
    text: string
    ephemeral?: boolean
    attachments?: TranscriptItem['attachments']
  }) => void
  updatePartialTranscript: (text: string | null) => void
  setLiveAssistantPreview: (preview: LiveAssistantPreview | null) => void
  clearLiveAssistantPreview: () => void
  toggleTranscriptItemCollapse: (id: string) => void
  setReconnectAttempt: (attempt: number) => void
  setSystemMetrics: (latency: number | null) => void
  setCoreName: (name: string) => void
  setContextMetrics: (metrics: ContextMetrics | null) => void
  setDiagnostics: (diagnostics: Diagnostics | null) => void
  setAudioDevices: (devices: AudioDeviceState) => void
  setAttentionState: (attentionState: AttentionState | null) => void
  setSessionState: (sessionState: JarvisSessionState) => void
  setPreferences: (preferences: JarvisPreferences) => void
  clearTranscript: () => void
  finalizeTranscriptByResponseId: (responseId: string) => void
  finalizeTranscriptByTurnId: (turnId: string) => void
  removeTranscriptByResponseId: (responseId: string) => void
  removeTranscriptByTurnId: (turnId: string) => void
  /** Exact-id removal for provisional barge-in candidate user rows. */
  removeTranscriptById: (id: string) => void
  toggleTranscript: () => void
  setPendingAttachment: (att: PendingAttachment | null) => void
  consumePendingAttachment: () => PendingAttachment | null
  
  // Widget Actions
  upsertWidget: (envelope: UIEnvelope) => void
  removeWidget: (widgetId: string) => void
  toggleWidgetPin: (widgetId: string) => void
  setActiveWidget: (widgetId: string | null) => void
  setWidgets: (widgets: Record<string, UIEnvelope>) => void
  markOperationsChanged: (scope: OperationsScope) => void
  markRunsChanged: () => void
  markPresenceChanged: () => void

  // Overlay actions
  openOverlay: (
    name:
      | 'integrations'
      | 'operations'
      | 'smart_home'
      | 'home_assistant'
      | 'presence'
      | 'settings',
    options?: {
      runKind?: OperationsRunsFilter['runKind']
      nodeId?: string | null
      nodeLabel?: string | null
      settingsSection?: 'audio' | 'model' | 'credentials' | 'host'
    },
  ) => void
  closeOverlay: () => void

  // Wake word feedback actions
  showWakewordFeedback: () => void
  hideWakewordFeedback: () => void
  setDevicePairingRequired: (required: boolean) => void
  setSetupState: (state: SetupState | null) => void
  setSetupLoading: (loading: boolean) => void
  setSetupRequired: (required: boolean) => void
}

export const useJarvisStore = create<JarvisState>((set, get) => ({
  connectionState: 'disconnected',
  hostState: 'unknown',
  agentState: 'idle',
  isSpeaking: false,
  isMuted: false,
  isAudioContextReady: false,
  transcript: [],
  partialTranscript: null,
  liveAssistantPreview: null,
  reconnectAttempt: 0,
  systemLatency: null,
  coreName: 'JARV1S',
  contextMetrics: null,
  diagnostics: null,
  widgets: {},
  activeWidgetId: null,
  operationsVersion: { automations: 0, protocols: 0, schedules: 0 },
  runsVersion: 0,
  presenceVersion: 0,
  isTranscriptVisible: false,
  pendingAttachment: null,
  audioDevices: defaultAudioDeviceState,
  attentionState: null,
  sessionState: { soft_muted: false },
  preferences: defaultPreferences,
  activeOverlay: null,
  operationsRunsFilter: null,
  settingsInitialSection: null,
  wakewordFeedbackVisible: false,
  devicePairingRequired: false,
  setupState: null,
  setupLoading: false,
  setupRequired: false,

  setConnectionState: (connectionState) => set({ connectionState }),
  setHostState: (hostState) => set({ hostState }),
  setAgentState: (agentState) => set({ agentState }),
  setIsSpeaking: (isSpeaking) => set({ isSpeaking }),
  setIsMuted: (isMuted) => set({ isMuted }),
  setIsAudioContextReady: (isAudioContextReady) => set({ isAudioContextReady }),
  addTranscriptItem: (item) => set((state) => ({ 
    transcript: [...state.transcript, { ...item, type: item.type || 'text' }] 
  })),
  setUserTranscriptPreview: ({ messageId, text, ephemeral }) => set((state) => {
    if (messageId) {
      const existingIndex = state.transcript.findIndex(
        (t) => t.id === messageId && t.type === 'text' && t.sender === 'user',
      )
      const preview: TranscriptItem = {
        id: messageId,
        text,
        sender: 'user',
        type: 'text',
        timestamp: existingIndex >= 0 ? state.transcript[existingIndex].timestamp : Date.now(),
        isPartial: true,
        ephemeral,
      }
      if (existingIndex >= 0) {
        const transcript = [...state.transcript]
        transcript[existingIndex] = { ...transcript[existingIndex], ...preview }
        return { transcript, partialTranscript: null }
      }
      return {
        transcript: [...state.transcript, preview],
        partialTranscript: null,
      }
    }

    return { partialTranscript: text }
  }),
  commitUserTranscript: ({ id, text, ephemeral, attachments }) => set((state) => {
    const committed: TranscriptItem = {
      id,
      text,
      sender: 'user',
      type: 'text',
      timestamp: Date.now(),
      isPartial: false,
      ephemeral,
      attachments,
    }

    const index = state.transcript.findIndex(
      (t) => t.id === id && t.type === 'text' && t.sender === 'user',
    )
    if (index >= 0) {
      const transcript = [...state.transcript]
      const existing = transcript[index]
      transcript[index] = {
        ...existing,
        ...committed,
        id,
        timestamp: existing.timestamp,
        ephemeral: ephemeral ?? existing.ephemeral,
      }
      return { transcript, partialTranscript: null }
    }

    return {
      transcript: [...state.transcript, committed],
      partialTranscript: null,
    }
  }),
  updateOrAddTranscriptItem: (item) => set((state) => {
    // If toolCallId provided (for code items)
    if (item.toolCallId && item.type === 'code') {
      const existingIndex = state.transcript.findIndex(
        t => t.toolCallId === item.toolCallId && t.type === 'code'
      )
      
      if (existingIndex >= 0) {
        const newTranscript = [...state.transcript]
        newTranscript[existingIndex] = {
          ...newTranscript[existingIndex],
          ...item,
          timestamp: newTranscript[existingIndex].timestamp
        }
        return { transcript: newTranscript }
      }
    }

    // Match by response_id (streaming assistant responses) or by id (fast recovery transcript updates)
    const matchById = item.response_id && (item.type === 'text' || item.type === 'reasoning')
    const matchValue = matchById ? item.response_id : item.id
    if (matchValue) {
      const existingIndex = state.transcript.findIndex(
        t => (matchById ? t.response_id : t.id) === matchValue && t.type === item.type
      )
      if (existingIndex >= 0) {
        const newTranscript = [...state.transcript]
        const existing = newTranscript[existingIndex]
        newTranscript[existingIndex] = {
          ...existing,
          ...item,
          timestamp: existing.timestamp,
          ephemeral: item.ephemeral ?? existing.ephemeral,
        }
        return { transcript: newTranscript }
      }
    }

    // Otherwise append new item
    return { transcript: [...state.transcript, { ...item, type: item.type || 'text' }] }
  }),
  updatePartialTranscript: (text) => set({ partialTranscript: text }),
  setLiveAssistantPreview: (liveAssistantPreview) => set({ liveAssistantPreview }),
  clearLiveAssistantPreview: () => set({ liveAssistantPreview: null }),
  toggleTranscriptItemCollapse: (id) => set((state) => ({
    transcript: state.transcript.map(item => 
      item.id === id ? { ...item, isCollapsed: !item.isCollapsed } : item
    )
  })),
  setReconnectAttempt: (reconnectAttempt) => set({ reconnectAttempt }),
  setSystemMetrics: (systemLatency) => set({ systemLatency }),
  setCoreName: (coreName) => set({ coreName }),
  setContextMetrics: (contextMetrics) => set({ contextMetrics }),
  setDiagnostics: (diagnostics) => set({ diagnostics }),
  setAudioDevices: (audioDevices) => set({ audioDevices }),
  setAttentionState: (attentionState) => set({ attentionState }),
  setSessionState: (sessionState) => set((state) => {
    const leavingSoftMute = state.sessionState.soft_muted && !sessionState.soft_muted
    if (!leavingSoftMute) {
      return { sessionState }
    }
    return {
      sessionState,
      partialTranscript: null,
      liveAssistantPreview: null,
      transcript: state.transcript.filter((item) => !item.ephemeral),
    }
  }),
  setPreferences: (preferences) => set({ preferences }),
  clearTranscript: () => set({
    transcript: [],
    partialTranscript: null,
    liveAssistantPreview: null,
  }),
  finalizeTranscriptByResponseId: (responseId) => set((state) => ({
    transcript: state.transcript.map(item =>
      item.response_id === responseId ? { ...item, isPartial: false } : item
    ),
  })),
  finalizeTranscriptByTurnId: (turnId) => set((state) => ({
    transcript: state.transcript.map(item =>
      item.turn_id === turnId && item.sender === 'assistant' && item.type === 'text'
        ? { ...item, isPartial: false }
        : item
    ),
  })),
  removeTranscriptByResponseId: (responseId) => set((state) => ({
    transcript: state.transcript.filter(t => t.response_id !== responseId),
    liveAssistantPreview: state.liveAssistantPreview?.key === responseId
      ? null
      : state.liveAssistantPreview,
  })),
  removeTranscriptByTurnId: (turnId) => set((state) => ({
    transcript: state.transcript.filter(t =>
      !(t.turn_id === turnId && t.sender === 'assistant' && t.type === 'text')
    ),
    liveAssistantPreview: state.liveAssistantPreview?.turnId === turnId
      ? null
      : state.liveAssistantPreview,
  })),
  removeTranscriptById: (id) => set((state) => ({
    transcript: state.transcript.filter((t) => t.id !== id),
    partialTranscript: null,
  })),
  toggleTranscript: () => set((state) => ({ isTranscriptVisible: !state.isTranscriptVisible })),
  setPendingAttachment: (pendingAttachment) => set({ pendingAttachment }),
  consumePendingAttachment: () => {
    const att = get().pendingAttachment
    if (att) set({ pendingAttachment: null })
    return att
  },

  upsertWidget: (envelope) => set((state) => ({
    widgets: { ...state.widgets, [envelope.widget_id]: envelope }
  })),
  removeWidget: (widgetId) => set((state) => {
    const newWidgets = { ...state.widgets }
    delete newWidgets[widgetId]
    return {
      widgets: newWidgets,
      activeWidgetId: state.activeWidgetId === widgetId ? null : state.activeWidgetId,
    }
  }),
  toggleWidgetPin: (widgetId) => set((state) => {
    const widget = state.widgets[widgetId];
    if (!widget) return state;
    return {
      widgets: {
        ...state.widgets,
        [widgetId]: { ...widget, pinned: !widget.pinned }
      }
    };
  }),
  setActiveWidget: (activeWidgetId) => set({ activeWidgetId }),
  setWidgets: (widgets) => set((state) => ({
    widgets,
    activeWidgetId: state.activeWidgetId && widgets[state.activeWidgetId]
      ? state.activeWidgetId
      : null,
  })),

  markOperationsChanged: (scope) => set((state) => ({
    operationsVersion: {
      ...state.operationsVersion,
      [scope]: state.operationsVersion[scope] + 1,
    },
  })),

  markRunsChanged: () => set((state) => ({ runsVersion: state.runsVersion + 1 })),
  markPresenceChanged: () => set((state) => ({ presenceVersion: state.presenceVersion + 1 })),

  openOverlay: (name, options) => set({
    activeOverlay: name,
    operationsRunsFilter: name === 'operations'
      ? (options
        ? { runKind: options.runKind ?? 'user', nodeId: options.nodeId ?? null, nodeLabel: options.nodeLabel ?? null }
        : null)
      : null,
    settingsInitialSection: name === 'settings' ? options?.settingsSection ?? null : null,
  }),
  closeOverlay: () => set({
    activeOverlay: null,
    operationsRunsFilter: null,
    settingsInitialSection: null,
  }),

  showWakewordFeedback: () => set({ wakewordFeedbackVisible: true }),
  hideWakewordFeedback: () => set({ wakewordFeedbackVisible: false }),
  setDevicePairingRequired: (devicePairingRequired) => set({ devicePairingRequired }),
  setSetupState: (setupState) => set({ setupState }),
  setSetupLoading: (setupLoading) => set({ setupLoading }),
  setSetupRequired: (setupRequired) => set({ setupRequired }),
}))
