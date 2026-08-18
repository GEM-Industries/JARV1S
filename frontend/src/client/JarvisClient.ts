import { useJarvisStore, type ContextMetrics, type Diagnostics } from '../store/useJarvisStore'
import { WSMessage, WSMessageType, WSResponse, AgentState, AudioDeviceKind, AudioDeviceOption, AudioDeviceState, type AttentionState, type AudioProcessingProfile, type JarvisPreferences, type JarvisSessionState } from '../types'
import { preferencesApi } from './preferencesApi'
import { tryCatch, ok, err, Result } from '../utils/result'
import { getRealtimeConnection, type RealtimeConnection } from './RealtimeConnection'
import {
  mintWsTicket,
  pairDevice,
} from './deviceAuthApi'
import { authorizedFetch } from './http'
import { dispatchAuthOAuthChanged } from '../runtime/authEvents'
import { getClientSurface, isPhoneCompanion, suggestedPhoneName } from '../runtime/clientSurface'
import {
  getCallActivity,
  listenForCallActivity,
  type CallActivity,
} from '../runtime/desktopBridge'
import { resolveDeviceGps } from '../runtime/deviceLocation'
import {
  buildMicConstraints,
  DEFAULT_AUDIO_PROCESSING_PROFILE,
  isAudioProcessingProfile,
  readAppliedEchoCancellation,
  resolveAudioProcessingProfile,
} from './audioCapturePolicy'
import { PcmClipCapture } from './pcmClipCapture'
import {
  getClientDiagnosticsRecorder,
  micCaptureStallReason,
  micFlatlineReason,
  notePlaybackChunk,
  playbackSummaryMetadata,
  type PlaybackTurnStats,
} from './clientDiagnostics'

const WORKLET_CODE = `
class AudioProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.bufferSize = 1536;
    this.buffer = new Int16Array(this.bufferSize);
    this.bufferIndex = 0;
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate;
    this.readOffset = 0;
    this.prevSample = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input.length) return true;
    const channelData = input[0];
    if (!channelData || channelData.length === 0) return true;

    // Capture at the context's native rate, then downsample to the backend's 16 kHz contract.
    let position = this.readOffset;
    while (position < channelData.length) {
      const index = Math.floor(position);
      const frac = position - index;
      const current = channelData[index] ?? this.prevSample;
      const next = channelData[Math.min(index + 1, channelData.length - 1)] ?? current;
      const sample = current + (next - current) * frac;
      const s = Math.max(-1, Math.min(1, sample));
      this.buffer[this.bufferIndex++] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      if (this.bufferIndex === this.bufferSize) {
        this.port.postMessage(this.buffer.slice());
        this.bufferIndex = 0;
      }
      position += this.ratio;
    }
    this.readOffset = position - channelData.length;
    this.prevSample = channelData[channelData.length - 1] ?? this.prevSample;
    return true;
  }
}
registerProcessor('audio-processor', AudioProcessor);
`;

const STT_SAMPLE_RATE = 16_000
const DEFAULT_TTS_SAMPLE_RATE = 24_000

type SinkSelectableAudioContext = AudioContext & {
  setSinkId?: (sinkId: string | { type: 'none' }) => Promise<void>
}

type AudioSessionNavigator = Navigator & {
  audioSession?: {
    type: 'auto' | 'playback' | 'play-and-record'
  }
}

type QueuedAudioChunk = {
  data: string
  sampleRate: number
  turnId?: string
}

type AudioCuePhase = 'start' | 'done'

class JarvisClient {
  private static instance: JarvisClient
  private readonly connection: RealtimeConnection
  
  // Audio State
  private audioContext: AudioContext | null = null
  private playbackContext: AudioContext | null = null
  private processor: AudioWorkletNode | null = null
  private micStream: MediaStream | null = null
  private micSource: MediaStreamAudioSourceNode | null = null
  private activeSources: AudioBufferSourceNode[] = []
  private previewSource: AudioBufferSourceNode | null = null
  private audioQueue: QueuedAudioChunk[] = []
  private activePlaybackTurnId: string | null = null
  private isPlayingQueue = false
  private nextStartTime = 0
  private phoneTtsCaptureBlocked = false
  private phoneTtsResumeTimer: ReturnType<typeof setTimeout> | null = null
  private parkedMicReleaseTimer: ReturnType<typeof setTimeout> | null = null
  private readonly MIN_BUFFER_CHUNKS = 1
  private readonly PHONE_TTS_ECHO_GUARD_MS = 400
  private readonly PHONE_MIC_PARK_MS = 60_000

  // Notification Sound State
  private notificationSounds: Map<string, { audio: HTMLAudioElement; gain: GainNode }> = new Map()
  private cueSounds: Map<AudioCuePhase, { audio: HTMLAudioElement; gain: GainNode }> = new Map()
  private activeNotification: string | null = null
  private static readonly SOUND_REGISTRY: Record<string, { file: string; behavior: 'play_once' | 'loop' }> = {
    chime: { file: '/sounds/chime.wav', behavior: 'play_once' },
    alarm: { file: '/sounds/alarm.wav', behavior: 'loop' },
    timer: { file: '/sounds/timer.wav', behavior: 'play_once' },
  }
  private static readonly CUE_REGISTRY: Record<AudioCuePhase, { file: string }> = {
    start: { file: '/sounds/tool_start.wav' },
    done: { file: '/sounds/tool_done.wav' },
  }

  private readonly NODE_ID_STORAGE_KEY = 'jarvis.node_id'
  private readonly AUDIO_INPUT_STORAGE_PREFIX = 'jarvis.audio.input'
  private readonly AUDIO_OUTPUT_STORAGE_PREFIX = 'jarvis.audio.output'
  private readonly AUDIO_MUTED_STORAGE_PREFIX = 'jarvis.audio.muted'
  private readonly AUDIO_PROCESSING_STORAGE_PREFIX = 'jarvis.audio.processing'
  private isDeviceChangeBound = false
  private callActivityBinding = false
  private callActivityUnlisten: (() => void) | null = null
  private readonly diagnostics = getClientDiagnosticsRecorder()

  // Mic liveness: one-shot acquire flatline + ongoing stall while unmuted.
  private micFramesSinceAcquire = 0
  private micPeakSinceAcquire = 0
  private micLastFrameAt: number | null = null
  private micLivenessTimer: ReturnType<typeof setTimeout> | null = null
  private micStallTimer: ReturnType<typeof setInterval> | null = null
  private micFlatlineEmitted = false
  private readonly intentionallyStoppedMicTracks = new WeakSet<MediaStreamTrack>()
  private readonly MIC_LIVENESS_MS = 3000
  private readonly MIC_STALL_MS = 2500
  private readonly MIC_STALL_POLL_MS = 1500
  private readonly MIC_FLATLINE_PEAK = 80
  private micForegroundBusy = false

  // Per-turn playback summary aggregation.
  private playbackTurnStats: PlaybackTurnStats | null = null
  private lastPlaybackContextState: string | null = null
  private playbackContextBound = false

  // Bounded local PCM capture for setup flows (wake check, speaker enrollment).
  private pcmClipCapture: PcmClipCapture | null = null
  private pcmClipMicWasTemporary = false

  // Wake word feedback state
  private _wakeAudioPending = false
  private _responseDelivered = false
  private _dismissTimer: number | null = null
  private _visibilityListenerBound = false

  private constructor() {
    this.connection = getRealtimeConnection()
    useJarvisStore.getState().setIsMuted(this.readPersistedMute())
    this.updateAudioDeviceState({ processingProfile: this.readProcessingProfile() })
    this.connection.configure({
      buildUrl: () => this.buildWebSocketUrl(),
      onOpen: () => {
        useJarvisStore.getState().setDevicePairingRequired(false)
        useJarvisStore.getState().clearLiveAssistantPreview()
        useJarvisStore.getState().updatePartialTranscript(null)
        if (useJarvisStore.getState().isMuted) {
          this.sendMessage('audio.mute', {})
        }
        this.sendLocationContext().catch(console.error)
        this.loadHistory()
      },
      onMessage: (message) => this.handleMessage(message),
      onAuthRequired: () => {
        useJarvisStore.getState().setDevicePairingRequired(true)
      },
    })
    this.bindVisibilityLocationRefresh()
    void this.bindCallActivity()
  }

  public static getInstance(): JarvisClient {
    const jarvisGlobal = globalThis as typeof globalThis & { __jarvisClient?: JarvisClient }
    if (!jarvisGlobal.__jarvisClient) {
      jarvisGlobal.__jarvisClient = new JarvisClient()
    }
    JarvisClient.instance = jarvisGlobal.__jarvisClient
    return JarvisClient.instance
  }

  // --- WebSocket ---

  public connect(isRetry = false): void {
    void this.bindCallActivity()
    this.connection.connect(isRetry)
  }

  public async pairWithCode(code: string): Promise<void> {
    const pageParams = new URLSearchParams(window.location.search)
    await pairDevice({
      code,
      node_id: this.getOrCreateNodeId(),
      node_label:
        pageParams.get('node_label') ||
        pageParams.get('label') ||
        (isPhoneCompanion() ? suggestedPhoneName() : undefined),
      capabilities: 'mic,speaker,display',
      client_surface: getClientSurface(),
      room_id: pageParams.get('room_id') || pageParams.get('room') || undefined,
      room_name: pageParams.get('room_name') || undefined,
      ha_area_id: pageParams.get('ha_area_id') || undefined,
      ha_device_id: pageParams.get('ha_device_id') || undefined,
      ha_entity_id: pageParams.get('ha_entity_id') || undefined,
      location_provider: pageParams.get('ha_area_id') ? 'home_assistant' : undefined,
    })
    useJarvisStore.getState().setDevicePairingRequired(false)
    this.connect()
  }

  public disconnect(): void {
    this._clearWakewordFeedback()
    this.callActivityUnlisten?.()
    this.callActivityUnlisten = null
    this.connection.disconnect()
    this.stopAudioCapture()
    useJarvisStore.getState().setContextMetrics(null)
  }

  public sendTextMessage(text: string): Result<void> {
    useJarvisStore.getState().clearLiveAssistantPreview()
    return this.sendMessage('user_text', { text })
  }

  public sendMessage(type: WSMessageType, data: Record<string, unknown>): Result<void> {
    const message: WSMessage = {
      id: `web-${Date.now()}`,
      type,
      data: data,
    }

    return this.connection.send(message)
  }

  private isLocalDevBypassHost(): boolean {
    const host = window.location.hostname
    return host === 'localhost' || host === '127.0.0.1'
  }

  private async buildWebSocketUrl(): Promise<string> {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const presence = this.buildPresenceQuery()
    try {
      const ticketResponse = await mintWsTicket()
      const params = new URLSearchParams(presence)
      params.set('ticket', ticketResponse.ticket)
      return `${protocol}//${window.location.host}/api/v1/ws?${params.toString()}`
    } catch {
      if (this.isLocalDevBypassHost()) {
        return `${protocol}//${window.location.host}/api/v1/ws?${presence}`
      }
      throw new Error('pairing_required')
    }
  }

  private buildPresenceQuery(): string {
    const pageParams = new URLSearchParams(window.location.search)
    const params = new URLSearchParams({
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      node_id: this.getOrCreateNodeId(),
      capabilities: 'mic,speaker,display',
      client_surface: getClientSurface(),
    })

    const nodeLabel = pageParams.get('node_label') || pageParams.get('label')
    if (nodeLabel) params.set('node_label', nodeLabel)

    const roomId = pageParams.get('room_id') || pageParams.get('room')
    const roomName = pageParams.get('room_name')
    if (roomId) {
      params.set('room_id', roomId)
      params.set('location_provider', 'manual')
    }
    if (roomName) {
      params.set('room_name', roomName)
      params.set('location_provider', 'manual')
    }

    for (const key of ['ha_area_id', 'ha_device_id', 'ha_entity_id']) {
      const value = pageParams.get(key)
      if (value) {
        params.set(key, value)
        params.set('location_provider', 'home_assistant')
      }
    }

    return params.toString()
  }

  getNodeId(): string {
    return this.getOrCreateNodeId()
  }

  private getOrCreateNodeId(): string {
    const existing = window.localStorage.getItem(this.NODE_ID_STORAGE_KEY)
    if (existing) return existing

    const prefix = isPhoneCompanion() ? 'phone' : 'browser'
    const id = typeof crypto.randomUUID === 'function'
      ? `${prefix}-${crypto.randomUUID()}`
      : `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`
    window.localStorage.setItem(this.NODE_ID_STORAGE_KEY, id)
    return id
  }

  private handleMessage(msg: WSResponse): void {
    const store = useJarvisStore.getState()
    const attentionState = this.parseAttentionState(msg.data)
    if (attentionState) {
      store.setAttentionState(attentionState)
    }
    const sessionState = this.parseSessionState(msg.data)
    if (sessionState) {
      store.setSessionState(sessionState)
    }
    const preferences = this.parsePreferences(msg.data)
    if (preferences) {
      store.setPreferences(preferences)
    }

    switch (msg.type) {
      case 'system.connect':
        store.setAttentionState(this.parseAttentionState(msg.data))
        store.setSessionState(this.parseSessionState(msg.data) ?? { soft_muted: false })
        store.setPreferences(this.parsePreferences(msg.data) ?? {
          owner_id: '',
          audio: { tool_cues_enabled: true },
        })
        this.stopPlayback(false, false)
        store.setAgentState('idle')
        store.setIsSpeaking(false)
        store.updatePartialTranscript(null)
        store.clearLiveAssistantPreview()
        break

      case 'speech.start':
        if (msg.data?.wake_word) {
          this.interruptBackgroundAudio()
          store.clearLiveAssistantPreview()
          store.setAgentState('waking')
          // Start feedback capture window
          this._startWakewordFeedback()
          // Don't stop playback — user just said the wake word, not interrupting yet.
          // Backend handles turn cancellation via VOICE_WAKE if needed.
        } else if (!msg.data?.barge_candidate) {
          // Candidates may be playback echo; wait for a committed speech.start.
          this.interruptBackgroundAudio() // Committed user speech should stop alarms/timers.
          this.stopPlayback(false) // Barge-in: stop playback first, then set state
          store.clearLiveAssistantPreview()
          store.setAgentState('listening')
        }
        break

      case 'speech.end':
        // Backend usually sends status.update next
        break

      case 'status.update':
        if (msg.data?.stage) {
          const stage = msg.data.stage as AgentState
          store.setAgentState(stage)
          if (this._wakeAudioPending && stage === 'idle') {
            this._scheduleWakewordDismiss()
          }
        }
        break

      case 'preferences.update':
        // Parsed above so preferences bundled with any message share one path.
        break

      case 'conversation.partial':
        if (msg.data?.text) {
          const text = msg.data.text as string
          const messageId = this.getVoiceTranscriptId(msg.data)
          if (!messageId) break
          store.setUserTranscriptPreview({
            messageId,
            text,
            ephemeral: store.sessionState.soft_muted,
          })
        }
        break

      case 'conversation.transcript': {
        if (msg.data?.text) {
          const consumed = store.consumePendingAttachment()
          const messageId = this.getVoiceTranscriptId(msg.data)
          if (!messageId) break
          store.commitUserTranscript({
            id: messageId,
            text: msg.data.text as string,
            ephemeral: store.sessionState.soft_muted,
            attachments: consumed ? [{ type: 'image', url: consumed.dataUrl }] : undefined,
          })
          store.markRunsChanged()
        } else {
          store.updatePartialTranscript(null)
        }
        break
      }

        case 'conversation.response':
          // Handle streaming text response
          if (msg.data?.text && msg.data?.response_id) {
            const responseId = msg.data.response_id as string
            const turnId = typeof msg.data.turn_id === 'string' ? msg.data.turn_id : undefined
            const isPartial = msg.data.is_partial as boolean
            const text = msg.data.text as string

            store.setLiveAssistantPreview({
              text,
              key: responseId,
              turnId,
            })
            store.updateOrAddTranscriptItem({
              id: responseId,
              response_id: responseId,
              turn_id: turnId,
              text,
              sender: 'assistant',
              type: 'text',
              isPartial,
              timestamp: Date.now()
            })
          }
          break

        case 'conversation.no_reply':
          store.updateOrAddTranscriptItem({
            id: `${msg.message_id}:no_reply`,
            text: (msg.data?.text as string | undefined) ?? "Jarvis didn't reply.",
            sender: 'system',
            type: 'notice',
            timestamp: Date.now()
          })
          break

        case 'conversation.code':
          if (msg.data?.text && msg.data?.tool_call_id) {
            const toolCallId = msg.data.tool_call_id as string
            store.updateOrAddTranscriptItem({
              id: toolCallId,
              toolCallId,
              code: msg.data.text as string,
              sender: 'assistant',
              type: 'code',
              status: 'running',
              isCollapsed: true,
              timestamp: Date.now()
            })
          }
          break

        case 'conversation.code_output':
          if (msg.data?.text && msg.data?.tool_call_id) {
            const toolCallId = msg.data.tool_call_id as string
            store.updateOrAddTranscriptItem({
              id: toolCallId,
              toolCallId,
              codeResult: msg.data.text as string,
              sender: 'assistant',
              type: 'code',
              status: 'completed',
              timestamp: Date.now()
            })
          }
          break

        case 'conversation.reasoning':
          if (msg.data?.text && msg.data?.response_id) {
            const responseId = msg.data.response_id as string
            const turnId = typeof msg.data.turn_id === 'string' ? msg.data.turn_id : undefined
            const existing = store.transcript.find(item => item.id === responseId && item.type === 'reasoning')
            const prior = existing?.text ?? ''
            store.updateOrAddTranscriptItem({
              id: responseId,
              response_id: responseId,
              turn_id: turnId,
              text: prior + (msg.data.text as string),
              sender: 'assistant',
              type: 'reasoning',
              isPartial: Boolean(msg.data.is_partial),
              isCollapsed: existing?.isCollapsed ?? true,
              timestamp: existing?.timestamp ?? Date.now(),
            })
          }
          break

      case 'notification.sound':
        if (msg.data?.sound) {
          this.playNotificationSound(msg.data.sound as string, msg.message_id)
        }
        break

      case 'audio.cue':
        if (msg.data?.phase === 'start' || msg.data?.phase === 'done') {
          this.playAudioCue(msg.data.phase, msg.message_id)
        }
        break

      case 'jarvis_audio':
        if (msg.data?.audio) {
          if (this._wakeAudioPending) this._responseDelivered = true
          const sampleRate =
            typeof msg.data.sample_rate === 'number'
              ? msg.data.sample_rate
              : DEFAULT_TTS_SAMPLE_RATE
          const turnId = typeof msg.data.turn_id === 'string' ? msg.data.turn_id : undefined
          this.notePlaybackReceive(turnId)
          this.playAudio(msg.data.audio as string, sampleRate, turnId)
        }
        break

      case 'audio.tts_end':
        // Satellite uses this marker to disambiguate TTS completion from local playback drain.
        break

      case 'conversation.retract':
        if (typeof msg.data?.message_id === 'string') {
          store.removeTranscriptById(msg.data.message_id)
        } else if (typeof msg.data?.turn_id === 'string') {
          store.removeTranscriptByTurnId(msg.data.turn_id)
        } else if (msg.data?.response_id) {
          store.removeTranscriptByResponseId(msg.data.response_id as string)
        }
        break

      case 'system.error':
        if (msg.data?.setup_required) {
          store.setSetupRequired(true)
          store.setAgentState('idle')
          this.stopPlayback(false)
        }
        break

      case 'system.stop':
        if (typeof msg.data?.turn_id === 'string') {
          store.finalizeTranscriptByTurnId(msg.data.turn_id)
        } else if (msg.data?.response_id) {
          store.finalizeTranscriptByResponseId(msg.data.response_id as string)
        }
        this.stopPlayback(false)
        break

      case 'system.clear_transcript':
        store.clearTranscript()
        break

      case 'system.pong': {
        if (msg.data?.core_id) {
          store.setCoreName(msg.data.core_id as string)
        }
        // Transport RTT is handled in RealtimeConnection; keep diagnostics here.
        if (msg.data?.diagnostics) {
          store.setDiagnostics(msg.data.diagnostics as Diagnostics)
        }
        break
      }

      case 'context.metrics':
        if (msg.data?.budget != null) {
          store.setContextMetrics(msg.data as unknown as ContextMetrics)
        }
        break

      case 'operations.changed': {
        const scope = msg.data?.scope
        if (scope === 'automations' || scope === 'protocols' || scope === 'schedules') {
          store.markOperationsChanged(scope)
        }
        break
      }

      case 'activity.changed':
        store.markRunsChanged()
        break

      case 'presence.changed':
        store.markPresenceChanged()
        break

      case 'task.update':
        store.markRunsChanged()
        break

      case 'auth.oauth.changed':
        if (typeof msg.data?.app === 'string') {
          dispatchAuthOAuthChanged({
            app: msg.data.app,
            success: Boolean(msg.data.success),
            loaded: typeof msg.data.loaded === 'boolean' ? msg.data.loaded : undefined,
            kind: msg.data.kind === 'composio' ? 'composio' : 'bespoke',
          })
        }
        break

      case 'ui.update':
        if (msg.data) {
          store.upsertWidget(msg.data as any)
        }
        break

      case 'ui.snapshot':
        if (Array.isArray(msg.data?.widgets)) {
          const widgets = Object.fromEntries(
            (msg.data.widgets as any[])
              .filter((widget) => typeof widget?.widget_id === 'string')
              .map((widget) => [widget.widget_id, widget])
          )
          store.setWidgets(widgets)
        }
        break

      case 'ui.delete':
        if (msg.data?.widget_id) {
          store.removeWidget(msg.data.widget_id as string)
        }
        break

    }
  }

  private getVoiceTranscriptId(data: Record<string, unknown> | undefined): string | undefined {
    if (!data) return undefined
    const turnId = data.turn_id
    if (typeof turnId === 'string' && turnId) return turnId
    return undefined
  }

  private parseAttentionState(data: Record<string, unknown> | undefined): AttentionState | null {
    const raw = data?.attention
    if (!raw || typeof raw !== 'object') return null

    const attention = raw as Record<string, unknown>
    const mode = attention.mode
    if (mode !== 'active' && mode !== 'quiet' && mode !== 'paused') return null

    return {
      owner_id: typeof attention.owner_id === 'string' ? attention.owner_id : '',
      mode,
      expires_at: typeof attention.expires_at === 'string' ? attention.expires_at : null,
      updated_at: typeof attention.updated_at === 'string' ? attention.updated_at : null,
    }
  }

  private parseSessionState(data: Record<string, unknown> | undefined): JarvisSessionState | null {
    const raw = data?.session
    if (!raw || typeof raw !== 'object') return null

    const session = raw as Record<string, unknown>
    return {
      soft_muted: session.soft_muted === true,
    }
  }

  private parsePreferences(data: Record<string, unknown> | undefined): JarvisPreferences | null {
    const raw = data?.preferences
    if (!raw || typeof raw !== 'object') return null

    const preferences = raw as Record<string, unknown>
    const audio = preferences.audio
    if (!audio || typeof audio !== 'object') return null
    const audioPrefs = audio as Record<string, unknown>

    return {
      owner_id: typeof preferences.owner_id === 'string' ? preferences.owner_id : '',
      audio: {
        tool_cues_enabled: audioPrefs.tool_cues_enabled !== false,
      },
    }
  }

  // --- Wake Word Feedback ---

  public sendWakewordFeedback(label: 'true_positive' | 'false_positive'): void {
    if (!this._wakeAudioPending) return
    this._clearWakewordFeedback()
    this.sendMessage('wakeword.feedback', { label })
  }

  private _startWakewordFeedback(): void {
    this._clearWakewordFeedback()
    this._wakeAudioPending = true
    this._responseDelivered = false
    useJarvisStore.getState().showWakewordFeedback()
  }

  private _scheduleWakewordDismiss(): void {
    if (this._dismissTimer !== null) return
    this._dismissTimer = window.setTimeout(() => {
      this._dismissTimer = null
      if (!this._wakeAudioPending) return
      if (
        this._responseDelivered &&
        useJarvisStore.getState().diagnostics?.voice?.wakeword_save_positive_feedback
      ) {
        this.sendMessage('wakeword.feedback', { label: 'true_positive' })
      }
      this._clearWakewordFeedback()
    }, 5000)
  }

  private _clearWakewordFeedback(): void {
    if (this._dismissTimer !== null) {
      clearTimeout(this._dismissTimer)
      this._dismissTimer = null
    }
    this._wakeAudioPending = false
    this._responseDelivered = false
    useJarvisStore.getState().hideWakewordFeedback()
  }

  private async loadHistory(): Promise<void> {
    const store = useJarvisStore.getState()
    if (store.transcript.length > 0) return

    try {
      const resp = await authorizedFetch('/api/v1/history/?limit=50')
      if (!resp.ok) return

      interface HistoryItem {
        role: string
        content: string
        timestamp: string
        type: 'text' | 'code' | 'notice' | 'reasoning'
        code?: string | null
        code_result?: string | null
        tool_call_id?: string | null
        response_id?: string | null
        turn_id?: string | null
      }

      const messages = await resp.json() as HistoryItem[]

      messages.forEach((m, i) => {
        const ts = new Date(m.timestamp).getTime()
        const sender = m.role === 'user' ? 'user' : 'assistant' as const

        if (m.type === 'code' && m.code) {
          const toolCallId = m.tool_call_id || `history-code-${i}-${m.timestamp}`
          store.addTranscriptItem({
            id: toolCallId,
            toolCallId,
            code: m.code,
            codeResult: m.code_result || undefined,
            sender: 'assistant',
            type: 'code',
            status: 'completed',
            isCollapsed: true,
            timestamp: ts,
          })
        } else if (m.type === 'notice' && m.content) {
          store.addTranscriptItem({
            id: `history-notice-${i}-${m.timestamp}`,
            text: m.content,
            type: 'notice',
            sender: 'system',
            timestamp: ts,
          })
        } else if (m.type === 'reasoning' && m.content) {
          const responseId = m.response_id || `history-reasoning-${i}-${m.timestamp}`
          store.addTranscriptItem({
            id: responseId,
            response_id: responseId,
            turn_id: m.turn_id || undefined,
            text: m.content,
            type: 'reasoning',
            sender: 'assistant',
            isCollapsed: true,
            timestamp: ts,
          })
        } else if (m.content) {
          store.addTranscriptItem({
            id: `history-${i}-${m.timestamp}`,
            text: m.content,
            type: 'text',
            sender,
            timestamp: ts,
          })
        }
      })
    } catch (e) {
      console.warn('Failed to load conversation history:', e)
    }
  }

  // --- Location Context ---

  private bindVisibilityLocationRefresh(): void {
    if (this._visibilityListenerBound || typeof document === 'undefined') {
      return
    }
    this._visibilityListenerBound = true
    const onVisible = () => {
      if (document.visibilityState !== 'visible') {
        return
      }
      void this.onForegroundAudio()
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
  }

  /** Resume TTS + reacquire capture when focus returns and the mic looks dead. */
  private async onForegroundAudio(): Promise<void> {
    if (this.micForegroundBusy) return
    this.micForegroundBusy = true
    try {
      void this.resumePlaybackContext(this.playbackContext)
      const store = useJarvisStore.getState()
      if (store.connectionState === 'connected') {
        this.sendLocationContext().catch(console.error)
      }
      if (store.isMuted || !this.audioContext || !this.processor) return
      try {
        await this.audioContext.resume()
      } catch (error) {
        console.warn('Failed to resume capture AudioContext:', error)
      }
      await this.ensureMicCaptureHealthy()
    } finally {
      this.micForegroundBusy = false
    }
  }

  private micTracksLive(): boolean {
    if (!this.micStream || !this.micSource) return false
    const tracks = this.micStream.getAudioTracks()
    return tracks.length > 0 && tracks.every((track) => track.readyState === 'live' && !track.muted)
  }

  private async ensureMicCaptureHealthy(): Promise<void> {
    if (useJarvisStore.getState().isMuted) return
    if (!this.audioContext || !this.processor) return

    const reason = micCaptureStallReason({
      muted: false,
      hasStream: Boolean(this.micStream),
      trackLive: this.micTracksLive(),
      lastFrameAt: this.micLastFrameAt,
      now: performance.now(),
      stallMs: this.MIC_STALL_MS,
    })
    if (!this.micStream || reason) {
      await this.acquireMic()
    }
  }

  private async bindCallActivity(): Promise<void> {
    if (this.callActivityBinding || this.callActivityUnlisten) return
    this.callActivityBinding = true
    try {
      this.callActivityUnlisten = await listenForCallActivity((activity) => {
        void this.applyCallActivity(activity)
      })
      const activity = await getCallActivity()
      if (activity) await this.applyCallActivity(activity)
    } catch (error) {
      console.warn('Could not monitor desktop call activity:', error)
    } finally {
      this.callActivityBinding = false
    }
  }

  private async applyCallActivity(activity: CallActivity): Promise<void> {
    const current = useJarvisStore.getState().audioDevices
    const automaticCallCompatibility = activity.supported && activity.active
    const profileChanged =
      current.automaticCallCompatibility !== automaticCallCompatibility

    this.updateAudioDeviceState({
      automaticCallCompatibility,
      activeCallApp: automaticCallCompatibility ? activity.app ?? 'Conference call' : null,
    })

    if (
      !profileChanged
      || !this.audioContext
      || !this.processor
      || useJarvisStore.getState().isMuted
    ) {
      return
    }
    await this.acquireMic()
  }

  private async sendLocationContext(): Promise<void> {
    const { location, unavailableReason } = await resolveDeviceGps()
    if (unavailableReason) {
      this.diagnostics.record('location_unavailable', {
        severity: 'warning',
        metadata: { reason: unavailableReason },
      })
    }
    this.sendMessage('context.update', { location })
  }

  // --- Audio ---

  /** TTS playback context (24 kHz). Separate from mic capture so STT stays at 16 kHz. */
  private ensurePlaybackContext(): AudioContext | null {
    if (this.playbackContext?.state === 'closed') {
      this.playbackContext = null
      this.playbackContextBound = false
      this.lastPlaybackContextState = null
    }
    if (this.playbackContext) return this.playbackContext

    const AudioContextClass =
      window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    if (!AudioContextClass) return null

    this.playbackContext = new AudioContextClass({ sampleRate: DEFAULT_TTS_SAMPLE_RATE })
    this.bindPlaybackContextDiagnostics(this.playbackContext)
    return this.playbackContext
  }

  private bindPlaybackContextDiagnostics(context: AudioContext): void {
    if (this.playbackContextBound) return
    this.playbackContextBound = true
    this.lastPlaybackContextState = context.state
    context.addEventListener('statechange', () => {
      const next = context.state
      const prev = this.lastPlaybackContextState
      this.lastPlaybackContextState = next
      // Only when leaving running — return is visible on the next playback_summary.
      if (prev !== 'running' || next === 'running') return
      if (!this.playbackTurnStats && this.activeSources.length === 0 && this.audioQueue.length === 0) {
        return
      }
      this.diagnostics.record('playback_failed', {
        severity: 'warning',
        turnId: this.activePlaybackTurnId ?? undefined,
        metadata: {
          reason: 'context_state',
          from: prev,
          to: next,
        },
      })
    })
  }

  /** WebKit/Tauri keeps playback contexts suspended until explicitly resumed. */
  private async ensurePlaybackReady(): Promise<AudioContext | null> {
    return this.resumePlaybackContext(this.ensurePlaybackContext())
  }

  private async resumePlaybackContext(context: AudioContext | null): Promise<AudioContext | null> {
    if (!context) return null
    if (context.state === 'running') return context
    try {
      await context.resume()
      return context
    } catch (error) {
      console.warn('Failed to resume TTS playback context:', error)
      this.diagnostics.record('playback_failed', {
        severity: 'error',
        turnId: this.activePlaybackTurnId ?? undefined,
        metadata: {
          reason: 'resume_failed',
          state: context.state,
        },
      })
      return null
    }
  }

  public async initAudio(): Promise<Result<void>> {
    if (this.audioContext && this.processor) {
      await this.audioContext.resume().catch(() => {})
      await this.ensurePlaybackReady()
      if (!useJarvisStore.getState().isMuted && (!this.micStream || !this.micSource)) {
        const micResult = await this.acquireMic()
        if (!micResult.ok) return micResult
      }
      await this.refreshAudioDevices()
      useJarvisStore.getState().setIsAudioContextReady(true)
      return ok(undefined)
    }

    const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    // Let WebKit use its native capture rate; the worklet owns conversion to 16 kHz.
    this.audioContext = new AudioContextClass()
    await this.ensurePlaybackReady()
    console.log(
      `Audio pipeline created: input ${this.audioContext.sampleRate}Hz→${STT_SAMPLE_RATE}Hz, TTS ${this.playbackContext?.sampleRate ?? DEFAULT_TTS_SAMPLE_RATE}Hz`
    )

    // Load AudioWorklet
    const blob = new Blob([WORKLET_CODE], { type: 'application/javascript' })
    const workletUrl = URL.createObjectURL(blob)
    try {
      await this.audioContext.audioWorklet.addModule(workletUrl)
    } finally {
      URL.revokeObjectURL(workletUrl)
    }

    this.processor = new AudioWorkletNode(this.audioContext, 'audio-processor')
    // Keep the graph alive without feeding mic samples to speakers.
    const silent = this.audioContext.createGain()
    silent.gain.value = 0
    this.processor.connect(silent)
    silent.connect(this.audioContext.destination)

    this.processor.port.onmessage = (e) => {
      const int16Data = e.data as Int16Array
      this.observeMicPcm(int16Data)

      if (this.pcmClipCapture) {
        this.pcmClipCapture.push(int16Data)
        return
      }

      if (!this.connection.isOpen()) return
      if (useJarvisStore.getState().isMuted) return
      if (isPhoneCompanion() && this.phoneTtsCaptureBlocked) return

      const bytes = new Uint8Array(int16Data.buffer)
      let binary = ''
      for (let i = 0; i < bytes.length; i++) {
        binary += String.fromCharCode(bytes[i])
      }

      this.sendMessage('user_audio', { audio: btoa(binary), encoding: 'base64' })
    }

    if (!useJarvisStore.getState().isMuted) {
      const micResult = await this.acquireMic()
      if (!micResult.ok) return micResult
    }

    await this.applyOutputDevice()
    this.preloadSounds()
    this.preloadCueSounds()
    this.bindDeviceChangeListener()
    await this.refreshAudioDevices()
    console.log('Audio pipeline initialized')
    useJarvisStore.getState().setIsAudioContextReady(true)
    return ok(undefined)
  }

  public async capturePcmClip(durationMs = 2200): Promise<Result<Uint8Array>> {
    if (this.pcmClipCapture !== null) {
      return err('Already recording a voice sample.')
    }

    // Reserve exclusive capture before acquiring the mic so early frames cannot leak to user_audio.
    const { capture, done } = PcmClipCapture.create({
      durationMs,
      sampleRate: STT_SAMPLE_RATE,
    })
    this.pcmClipCapture = capture
    this.pcmClipMicWasTemporary = useJarvisStore.getState().isMuted

    try {
      const ready = await this.initAudio()
      if (!ready.ok) {
        this.cancelOwnedPcmClipCapture(capture)
        return ready
      }
      if (!this.processor) {
        this.cancelOwnedPcmClipCapture(capture)
        return err('Audio processor is not ready.')
      }

      // Ensure the mic is actually feeding the worklet even if the UI is muted.
      if (!this.micSource || !this.micStream) {
        const micResult = await this.acquireMic()
        if (!micResult.ok) {
          this.cancelOwnedPcmClipCapture(capture)
          return micResult
        }
      }
      if (this.pcmClipCapture !== capture) {
        return err('Recording cancelled.')
      }
      capture.start()
    } catch (error) {
      this.cancelOwnedPcmClipCapture(capture)
      return err(error instanceof Error ? error.message : 'Could not start recording.')
    }

    const result = await done
    this.clearPcmClipCapture(capture)
    if (!result.ok) return err(result.error)
    return ok(result.pcm)
  }

  public cancelPcmClipCapture(): void {
    this.cancelOwnedPcmClipCapture(this.pcmClipCapture)
  }

  private cancelOwnedPcmClipCapture(capture: PcmClipCapture | null): void {
    if (!capture || this.pcmClipCapture !== capture) return
    capture.cancel()
    this.clearPcmClipCapture(capture)
  }

  private clearPcmClipCapture(capture: PcmClipCapture): void {
    if (this.pcmClipCapture !== capture) return
    const releaseTemporaryMic =
      this.pcmClipMicWasTemporary && useJarvisStore.getState().isMuted
    this.pcmClipCapture = null
    this.pcmClipMicWasTemporary = false
    if (releaseTemporaryMic) {
      this.releaseMic()
    }
  }

  public async refreshAudioDevices(): Promise<Result<void>> {
    if (!navigator.mediaDevices?.enumerateDevices) {
      this.updateAudioDeviceState({ error: 'Audio devices are not available in this browser.' })
      return err('Audio devices are not available in this browser.')
    }

    const devicesResult = await tryCatch(navigator.mediaDevices.enumerateDevices())
    if (!devicesResult.ok) {
      this.updateAudioDeviceState({ error: devicesResult.error })
      return err(devicesResult.error)
    }

    const devices = devicesResult.value
    const inputs = this.toAudioDeviceOptions(devices, 'audioinput')
    const outputs = this.toAudioDeviceOptions(devices, 'audiooutput')
    const selectedInputId = this.resolveSelectedDeviceId('input', inputs)
    const selectedOutputId = this.resolveSelectedDeviceId('output', outputs)

    const current = useJarvisStore.getState().audioDevices
    useJarvisStore.getState().setAudioDevices({
      ...current,
      inputs,
      outputs,
      selectedInputId,
      selectedOutputId,
      activeInputLabel: this.deviceLabel(inputs, selectedInputId),
      activeOutputLabel: this.deviceLabel(outputs, selectedOutputId),
      outputSelectionSupported: this.canSelectOutputDevice(),
      permissionGranted: devices.some((device) => Boolean(device.label)),
      processingProfile: this.readProcessingProfile(),
      error: null,
    })

    return ok(undefined)
  }

  public async selectAudioInput(deviceId: string): Promise<Result<void>> {
    this.persistSelectedAudioDevice('input', deviceId)
    this.updateAudioDeviceState({
      selectedInputId: deviceId,
      activeInputLabel: this.deviceLabel(useJarvisStore.getState().audioDevices.inputs, deviceId),
      error: null,
    })

    if (!this.audioContext || !this.processor || useJarvisStore.getState().isMuted) {
      return this.refreshAudioDevices()
    }

    const result = await this.acquireMic()
    await this.refreshAudioDevices()
    return result
  }

  public async selectAudioProcessingProfile(profile: AudioProcessingProfile): Promise<Result<void>> {
    this.persistProcessingProfile(profile)
    this.updateAudioDeviceState({ processingProfile: profile })

    if (
      useJarvisStore.getState().audioDevices.automaticCallCompatibility
      || !this.audioContext
      || !this.processor
      || useJarvisStore.getState().isMuted
    ) {
      return ok(undefined)
    }

    this.updateAudioDeviceState({ appliedEchoCancellation: null })
    return this.acquireMic()
  }

  public async selectAudioOutput(deviceId: string): Promise<Result<void>> {
    if (deviceId && !this.canSelectOutputDevice()) {
      const message = 'Speaker selection is controlled by this browser or operating system.'
      this.updateAudioDeviceState({ error: message, selectedOutputId: '', activeOutputLabel: 'Speaker · Default' })
      return err(message)
    }

    this.persistSelectedAudioDevice('output', deviceId)
    this.updateAudioDeviceState({
      selectedOutputId: deviceId,
      activeOutputLabel: this.deviceLabel(useJarvisStore.getState().audioDevices.outputs, deviceId),
      error: null,
    })

    const result = await this.applyOutputDevice(deviceId)
    await this.refreshAudioDevices()
    return result
  }

  public async testSpeaker(): Promise<Result<void>> {
    const audioResult = await this.initAudio()
    if (!audioResult.ok) return audioResult

    const context = await this.ensurePlaybackReady() ?? this.audioContext
    if (!context) return err('Audio playback is unavailable on this device.')

    try {
      const oscillator = context.createOscillator()
      const gain = context.createGain()
      const now = context.currentTime
      // Two clear mid beeps — easy to hear, hard to mistake for noise.
      oscillator.frequency.value = 880
      gain.gain.setValueAtTime(0, now)
      gain.gain.linearRampToValueAtTime(0.3, now + 0.02)
      gain.gain.linearRampToValueAtTime(0, now + 0.2)
      gain.gain.setValueAtTime(0, now + 0.32)
      gain.gain.linearRampToValueAtTime(0.3, now + 0.34)
      gain.gain.linearRampToValueAtTime(0, now + 0.55)
      oscillator.connect(gain)
      gain.connect(context.destination)

      await new Promise<void>((resolve) => {
        oscillator.onended = () => resolve()
        oscillator.start(now)
        oscillator.stop(now + 0.6)
      })
      return ok(undefined)
    } catch (error) {
      return err(error instanceof Error ? error.message : 'Could not play a test sound.')
    }
  }

  private async acquireMic(): Promise<Result<void>> {
    if (!this.audioContext || !this.processor) return err('Audio pipeline not initialized')

    this.releaseMic()
    const selectedInputId = this.readSelectedAudioDevice('input')
    const profile = this.effectiveProcessingProfile()
    const audio = buildMicConstraints(profile, selectedInputId || undefined)
    const callCompat = useJarvisStore.getState().audioDevices.automaticCallCompatibility

    const streamResult = await tryCatch(
      navigator.mediaDevices.getUserMedia({
        audio,
      })
    )

    if (!streamResult.ok) {
      console.error(`Microphone access denied: ${streamResult.error}`)
      this.updateAudioDeviceState({ error: streamResult.error })
      this.diagnostics.record('mic_acquire', {
        severity: 'error',
        metadata: {
          ok: false,
          profile,
          call_compat: callCompat,
          reason: 'get_user_media',
        },
      })
      return err(streamResult.error)
    }

    this.micStream = streamResult.value
    this.micSource = this.audioContext.createMediaStreamSource(this.micStream)
    this.micSource.connect(this.processor)
    const track = this.micStream.getAudioTracks()[0]
    const settings = track?.getSettings?.() ?? {}
    const appliedEchoCancellation = readAppliedEchoCancellation(settings)
    this.micStream.getAudioTracks().forEach((micTrack) => {
      micTrack.addEventListener('mute', () => {
        if (this.intentionallyStoppedMicTracks.has(micTrack)) return
        this.diagnostics.record('mic_interrupted', {
          severity: 'warning',
          metadata: { reason: 'track_mute' },
        })
        if (!isPhoneCompanion() || useJarvisStore.getState().isMuted) return
        this.updateAudioDeviceState({
          error: 'Safari paused the microphone. Tap Resume microphone to continue.',
        })
      })
      micTrack.addEventListener('unmute', () => {
        if (this.intentionallyStoppedMicTracks.has(micTrack)) return
        this.updateAudioDeviceState({ error: null })
      })
      micTrack.addEventListener('ended', () => {
        if (this.intentionallyStoppedMicTracks.has(micTrack)) return
        this.diagnostics.record('mic_interrupted', {
          severity: 'warning',
          metadata: { reason: 'track_ended' },
        })
        this.markMicCaptureStalled('track_dead')
      })
    })
    this.updateAudioDeviceState({
      error: null,
      captureStalled: false,
      appliedEchoCancellation,
    })
    this.setAudioSessionType('play-and-record')
    this.diagnostics.record('mic_acquire', {
      severity: 'info',
      metadata: {
        ok: true,
        profile,
        call_compat: callCompat,
        sample_rate: typeof settings.sampleRate === 'number' ? settings.sampleRate : null,
        channels: typeof settings.channelCount === 'number' ? settings.channelCount : null,
        echo_cancellation: appliedEchoCancellation,
        noise_suppression: typeof settings.noiseSuppression === 'boolean' ? settings.noiseSuppression : null,
        auto_gain: typeof settings.autoGainControl === 'boolean' ? settings.autoGainControl : null,
      },
    })
    this.beginMicLivenessWatch()
    return ok(undefined)
  }

  private clearMicLivenessTimers(): void {
    if (this.micLivenessTimer) {
      clearTimeout(this.micLivenessTimer)
      this.micLivenessTimer = null
    }
    if (this.micStallTimer) {
      clearInterval(this.micStallTimer)
      this.micStallTimer = null
    }
  }

  private beginMicLivenessWatch(): void {
    this.clearMicLivenessTimers()
    this.micFramesSinceAcquire = 0
    this.micPeakSinceAcquire = 0
    this.micLastFrameAt = null
    this.micFlatlineEmitted = false
    this.updateAudioDeviceState({ captureStalled: false })
    this.micLivenessTimer = setTimeout(() => {
      this.micLivenessTimer = null
      const reason = micFlatlineReason({
        emitted: this.micFlatlineEmitted,
        hasStream: Boolean(this.micStream),
        frames: this.micFramesSinceAcquire,
        peak: this.micPeakSinceAcquire,
        peakThreshold: this.MIC_FLATLINE_PEAK,
      })
      if (!reason) return
      this.micFlatlineEmitted = true
      if (reason === 'no_frames') {
        this.markMicCaptureStalled(reason)
      } else {
        this.diagnostics.record('mic_flatline', {
          severity: 'warning',
          metadata: {
            reason,
            frames: this.micFramesSinceAcquire,
            peak: this.micPeakSinceAcquire,
          },
        })
      }
    }, this.MIC_LIVENESS_MS)
    this.micStallTimer = setInterval(() => {
      this.pollMicCaptureStall()
    }, this.MIC_STALL_POLL_MS)
  }

  private pollMicCaptureStall(): void {
    const muted = useJarvisStore.getState().isMuted
    const reason = micCaptureStallReason({
      muted,
      hasStream: Boolean(this.micStream),
      trackLive: this.micTracksLive(),
      lastFrameAt: this.micLastFrameAt,
      now: performance.now(),
      stallMs: this.MIC_STALL_MS,
    })
    if (!reason) return
    this.markMicCaptureStalled(reason)
  }

  private markMicCaptureStalled(reason: string): void {
    if (useJarvisStore.getState().isMuted) return
    if (!this.micStream) return
    if (useJarvisStore.getState().audioDevices.captureStalled) return
    this.updateAudioDeviceState({ captureStalled: true })
    this.diagnostics.record('mic_flatline', {
      severity: 'warning',
      metadata: {
        reason: `stall_${reason}`,
        frames: this.micFramesSinceAcquire,
        peak: this.micPeakSinceAcquire,
      },
    })
  }

  private observeMicPcm(samples: Int16Array): void {
    if (!this.micStream) return
    this.micLastFrameAt = performance.now()
    if (useJarvisStore.getState().audioDevices.captureStalled) {
      this.updateAudioDeviceState({ captureStalled: false })
    }
    if (this.micFlatlineEmitted) return
    this.micFramesSinceAcquire += 1
    for (let i = 0; i < samples.length; i += 32) {
      const abs = samples[i] < 0 ? -samples[i] : samples[i]
      if (abs > this.micPeakSinceAcquire) this.micPeakSinceAcquire = abs
    }
  }

  private async applyOutputDevice(deviceId = this.readSelectedAudioDevice('output')): Promise<Result<void>> {
    const outputContext = this.playbackContext ?? this.audioContext
    if (!outputContext) return ok(undefined)

    const selectableContext = outputContext as SinkSelectableAudioContext
    if (!selectableContext.setSinkId) {
      if (!deviceId) return ok(undefined)
      const message = 'Speaker selection is not supported in this browser.'
      this.updateAudioDeviceState({ error: message, selectedOutputId: '', activeOutputLabel: 'Speaker · Default' })
      return err(message)
    }

    const result = await tryCatch(selectableContext.setSinkId(deviceId))
    if (!result.ok) {
      if (deviceId) {
        this.persistSelectedAudioDevice('output', '')
        this.updateAudioDeviceState({
          selectedOutputId: '',
          activeOutputLabel: 'Speaker · Default',
          error: null,
        })
        return this.applyOutputDevice('')
      }
      this.updateAudioDeviceState({ error: result.error })
      return err(result.error)
    }
    return ok(undefined)
  }

  private bindDeviceChangeListener(): void {
    if (this.isDeviceChangeBound || !navigator.mediaDevices?.addEventListener) return
    navigator.mediaDevices.addEventListener('devicechange', this.handleDeviceChange)
    this.isDeviceChangeBound = true
  }

  private unbindDeviceChangeListener(): void {
    if (!this.isDeviceChangeBound || !navigator.mediaDevices?.removeEventListener) return
    navigator.mediaDevices.removeEventListener('devicechange', this.handleDeviceChange)
    this.isDeviceChangeBound = false
  }

  private handleDeviceChange = (): void => {
    this.refreshAudioDevices().catch((error) => {
      console.warn('Failed to refresh audio devices:', error)
    })
  }

  private toAudioDeviceOptions(devices: MediaDeviceInfo[], kind: AudioDeviceKind): AudioDeviceOption[] {
    const fallbackLabel = kind === 'audioinput' ? 'Microphone' : 'Speaker'
    const defaultDeviceLabel = this.defaultDeviceLabel(devices, kind)
    const defaultLabel = defaultDeviceLabel
      ? `${defaultDeviceLabel} · Default`
      : `${fallbackLabel} · Default`
    const normalizedDefaultLabel = defaultDeviceLabel ? this.normalizeDeviceLabel(defaultDeviceLabel) : null
    const seen = new Set<string>()
    const options = devices
      .filter((device) => device.kind === kind && device.deviceId !== 'default')
      .filter((device) => {
        if (!device.deviceId || seen.has(device.deviceId)) return false
        if (normalizedDefaultLabel && this.normalizeDeviceLabel(device.label) === normalizedDefaultLabel) {
          return false
        }
        seen.add(device.deviceId)
        return true
      })
      .map((device, index) => ({
        deviceId: device.deviceId,
        label: device.label || `${fallbackLabel} ${index + 1}`,
        kind,
        isDefault: false,
      }))

    return [
      { deviceId: '', label: defaultLabel, kind, isDefault: true },
      ...options,
    ]
  }

  private resolveSelectedDeviceId(kind: 'input' | 'output', options: AudioDeviceOption[]): string {
    const selected = this.readSelectedAudioDevice(kind)
    return selected && options.some((option) => option.deviceId === selected) ? selected : ''
  }

  private deviceLabel(options: AudioDeviceOption[], deviceId: string): string {
    return options.find((device) => device.deviceId === deviceId)?.label
      ?? (options[0]?.kind === 'audioinput' ? 'Microphone · Default' : 'Speaker · Default')
  }

  private defaultDeviceLabel(devices: MediaDeviceInfo[], kind: AudioDeviceKind): string | null {
    const defaultDevice = devices.find((device) => device.kind === kind && device.deviceId === 'default')
    const label = defaultDevice?.label?.replace(/^default\s*[-–—:]\s*/i, '').trim()
    return label || null
  }

  private normalizeDeviceLabel(label: string): string {
    return label.trim().toLowerCase().replace(/\s+/g, ' ')
  }

  private readSelectedAudioDevice(kind: 'input' | 'output'): string {
    return window.localStorage.getItem(this.audioDeviceStorageKey(kind)) ?? ''
  }

  private persistSelectedAudioDevice(kind: 'input' | 'output', deviceId: string): void {
    const key = this.audioDeviceStorageKey(kind)
    if (deviceId) {
      window.localStorage.setItem(key, deviceId)
    } else {
      window.localStorage.removeItem(key)
    }
  }

  private audioDeviceStorageKey(kind: 'input' | 'output'): string {
    const prefix = kind === 'input' ? this.AUDIO_INPUT_STORAGE_PREFIX : this.AUDIO_OUTPUT_STORAGE_PREFIX
    return `${prefix}.${this.getOrCreateNodeId()}`
  }

  private readProcessingProfile(): AudioProcessingProfile {
    const stored = window.localStorage.getItem(this.processingProfileStorageKey())
    return isAudioProcessingProfile(stored) ? stored : DEFAULT_AUDIO_PROCESSING_PROFILE
  }

  private effectiveProcessingProfile(): AudioProcessingProfile {
    return resolveAudioProcessingProfile(
      this.readProcessingProfile(),
      useJarvisStore.getState().audioDevices.automaticCallCompatibility,
    )
  }

  private persistProcessingProfile(profile: AudioProcessingProfile): void {
    window.localStorage.setItem(this.processingProfileStorageKey(), profile)
  }

  private processingProfileStorageKey(): string {
    return `${this.AUDIO_PROCESSING_STORAGE_PREFIX}.${this.getOrCreateNodeId()}`
  }

  private canSelectOutputDevice(): boolean {
    if ((this.playbackContext as SinkSelectableAudioContext | null)?.setSinkId) return true
    if ((this.audioContext as SinkSelectableAudioContext | null)?.setSinkId) return true
    const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    return Boolean(AudioContextClass?.prototype && 'setSinkId' in AudioContextClass.prototype)
  }

  private updateAudioDeviceState(patch: Partial<AudioDeviceState>): void {
    const current = useJarvisStore.getState().audioDevices
    useJarvisStore.getState().setAudioDevices({ ...current, ...patch })
  }

  private releaseMic(): void {
    if (this.parkedMicReleaseTimer) {
      clearTimeout(this.parkedMicReleaseTimer)
      this.parkedMicReleaseTimer = null
    }
    this.clearMicLivenessTimers()
    this.micLastFrameAt = null
    this.micSource?.disconnect()
    this.micSource = null
    this.micStream?.getTracks().forEach((track) => {
      this.intentionallyStoppedMicTracks.add(track)
      track.stop()
    })
    this.micStream = null
    this.updateAudioDeviceState({ appliedEchoCancellation: null, captureStalled: false })
    this.setAudioSessionType('auto')
  }

  private pauseMic(): void {
    if (isPhoneCompanion()) {
      if (this.parkedMicReleaseTimer) clearTimeout(this.parkedMicReleaseTimer)
      this.parkedMicReleaseTimer = setTimeout(() => {
        if (useJarvisStore.getState().isMuted) this.releaseMic()
      }, this.PHONE_MIC_PARK_MS)
      return
    }
    this.micStream?.getAudioTracks().forEach((track) => {
      track.enabled = false
    })
    this.setAudioSessionType('playback')
  }

  private resumeMic(): boolean {
    if (!this.micSource || !this.micStream) return false
    const tracks = this.micStream.getAudioTracks()
    if (
      tracks.length === 0
      || tracks.some((track) => track.readyState !== 'live' || track.muted)
    ) {
      return false
    }
    if (this.parkedMicReleaseTimer) {
      clearTimeout(this.parkedMicReleaseTimer)
      this.parkedMicReleaseTimer = null
    }
    tracks.forEach((track) => {
      track.enabled = true
    })
    this.updateAudioDeviceState({ error: null })
    this.setAudioSessionType('play-and-record')
    return true
  }

  private setAudioSessionType(type: 'auto' | 'playback' | 'play-and-record'): void {
    const audioSession = (navigator as AudioSessionNavigator).audioSession
    if (audioSession) audioSession.type = type
  }

  private blockPhoneMicForTts(): void {
    if (!isPhoneCompanion()) return
    if (this.phoneTtsResumeTimer) {
      clearTimeout(this.phoneTtsResumeTimer)
      this.phoneTtsResumeTimer = null
    }
    this.phoneTtsCaptureBlocked = true
  }

  private releasePhoneMicAfterTts(): void {
    if (!isPhoneCompanion()) return
    if (this.phoneTtsResumeTimer) clearTimeout(this.phoneTtsResumeTimer)
    this.phoneTtsResumeTimer = setTimeout(() => {
      this.phoneTtsCaptureBlocked = false
      this.phoneTtsResumeTimer = null
    }, this.PHONE_TTS_ECHO_GUARD_MS)
  }

  public toggleMute(): void {
    void this.toggleMuteAsync()
  }

  public async startPushToTalk(): Promise<Result<void>> {
    if (useJarvisStore.getState().connectionState !== 'connected') {
      return err('JARV1S is not connected.')
    }

    const ready = await this.initAudio()
    if (!ready.ok) return ready

    this.setMuted(false)
    if (!this.resumeMic()) {
      const mic = await this.acquireMic()
      if (!mic.ok) {
        this.setMuted(true)
        return mic
      }
    }
    this.sendMessage('voice.activate', {})
    return ok(undefined)
  }

  public stopPushToTalk(): void {
    this.setMuted(true)
    this.pauseMic()
    this.sendMessage('voice.commit', {})
  }

  private async toggleMuteAsync(): Promise<void> {
    let store = useJarvisStore.getState()
    if (!store.isAudioContextReady) {
      const result = await this.initAudio()
      if (!result.ok) {
        console.error('Failed to initialize audio:', result.error)
        return
      }
      store = useJarvisStore.getState()
      if (!store.isMuted) return
    }

    const nowMuted = !store.isMuted
    this.setMuted(nowMuted)
    if (nowMuted) {
      this.releaseMic()
      this.sendMessage('audio.mute', {})
    } else {
      const result = await this.acquireMic()
      if (!result.ok) {
        this.setMuted(true)
        console.error('Failed to re-acquire mic on unmute:', result.error)
      }
    }
  }

  private setMuted(muted: boolean): void {
    window.localStorage.setItem(this.audioMutedStorageKey(), String(muted))
    useJarvisStore.getState().setIsMuted(muted)
  }

  private readPersistedMute(): boolean {
    return window.localStorage.getItem(this.audioMutedStorageKey()) === 'true'
  }

  private audioMutedStorageKey(): string {
    return `${this.AUDIO_MUTED_STORAGE_PREFIX}.${this.getOrCreateNodeId()}`
  }

  public stopAudioCapture(): void {
    this.cancelPcmClipCapture()
    this.releaseMic()
    this.unbindDeviceChangeListener()
    if (this.processor) {
      this.processor.disconnect()
      this.processor = null
    }
    useJarvisStore.getState().setIsAudioContextReady(false)
  }

  public isToolCuesEnabled(): boolean {
    return useJarvisStore.getState().preferences.audio.tool_cues_enabled
  }

  public async setToolCuesEnabled(enabled: boolean): Promise<void> {
    useJarvisStore.getState().setPreferences({
      ...useJarvisStore.getState().preferences,
      audio: { tool_cues_enabled: enabled },
    })
    const preferences = await preferencesApi.setToolCuesEnabled(enabled)
    useJarvisStore.getState().setPreferences(preferences)
  }

  // --- Notification Sounds ---

  private preloadSounds(): void {
    const context = this.playbackContext ?? this.audioContext
    if (!context) return
    for (const [name, config] of Object.entries(JarvisClient.SOUND_REGISTRY)) {
      const audio = new Audio(config.file)
      audio.preload = 'auto'
      const source = context.createMediaElementSource(audio)
      const gain = context.createGain()
      source.connect(gain)
      gain.connect(context.destination)
      this.notificationSounds.set(name, { audio, gain })
    }
  }

  private preloadCueSounds(): void {
    const context = this.playbackContext ?? this.audioContext
    if (!context || this.cueSounds.size > 0) return
    for (const [phase, config] of Object.entries(JarvisClient.CUE_REGISTRY) as Array<[AudioCuePhase, { file: string }]>) {
      const audio = new Audio(config.file)
      audio.preload = 'auto'
      const source = context.createMediaElementSource(audio)
      const gain = context.createGain()
      gain.gain.value = 0.8
      source.connect(gain)
      gain.connect(context.destination)
      this.cueSounds.set(phase, { audio, gain })
    }
  }

  private playAudioCue(phase: AudioCuePhase, messageId?: string): void {
    if (!this.isToolCuesEnabled()) return
    this.preloadCueSounds()
    const entry = this.cueSounds.get(phase)
    if (!entry) return
    entry.audio.currentTime = 0
    entry.audio.play().catch(() => {
      this.diagnostics.record('notification_failed', {
        severity: 'error',
        messageId,
        metadata: { kind: `cue_${phase}`, reason: 'play_rejected' },
      })
    })
  }

  private playNotificationSound(sound: string, messageId?: string): void {
    this.stopNotificationSound()
    if (!this.notificationSounds.has(sound)) {
      this.diagnostics.record('notification_failed', {
        severity: 'error',
        messageId,
        metadata: { kind: sound, reason: 'unknown_sound' },
      })
      return
    }
    const key = sound
    const entry = this.notificationSounds.get(key)
    if (!entry) return

    const config = JarvisClient.SOUND_REGISTRY[key]
    entry.audio.currentTime = 0
    entry.gain.gain.value = 1.0
    entry.audio.loop = config.behavior === 'loop'
    if (config.behavior === 'play_once') {
      entry.audio.onended = () => { this.activeNotification = null }
    }
    entry.audio.play().catch(() => {
      this.diagnostics.record('notification_failed', {
        severity: 'error',
        messageId,
        metadata: { kind: key, reason: 'play_rejected' },
      })
    })
    this.activeNotification = key
  }

  private duckNotificationSound(): void {
    const context = this.playbackContext ?? this.audioContext
    if (!this.activeNotification || !context) return
    const entry = this.notificationSounds.get(this.activeNotification)
    if (!entry) return
    entry.gain.gain.setTargetAtTime(0.15, context.currentTime, 0.1)
  }

  private unduckNotificationSound(): void {
    const context = this.playbackContext ?? this.audioContext
    if (!this.activeNotification || !context) return
    const entry = this.notificationSounds.get(this.activeNotification)
    if (!entry) return
    entry.gain.gain.setTargetAtTime(1.0, context.currentTime, 0.3)
  }

  private stopNotificationSound(): void {
    if (!this.activeNotification) return
    const entry = this.notificationSounds.get(this.activeNotification)
    if (entry) {
      entry.audio.pause()
      entry.audio.currentTime = 0
      entry.audio.loop = false
    }
    this.activeNotification = null
  }

  // --- Interruption Handling ---

  /** Stops alarms, timers, and future media (Spotify, etc) */
  public interruptBackgroundAudio(): void {
    this.stopNotificationSound()
    // Future: this.pauseMediaPlayback()
    // Future: this.clearVisualToasts()
  }

  // --- TTS Audio Playback ---

  private decodePcmF32leBase64(base64Data: string): Float32Array {
    const binary = atob(base64Data)
    const bytes = new Uint8Array(binary.length)
    for (let i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i)
    }
    if (bytes.byteLength % 4 !== 0) {
      throw new Error(`PCM chunk length must be a multiple of 4 bytes, got ${bytes.byteLength}`)
    }
    return new Float32Array(new Float32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4))
  }

  private notePlaybackReceive(turnId?: string): void {
    this.playbackTurnStats = notePlaybackChunk(
      this.playbackTurnStats,
      turnId ?? null,
      performance.now(),
      this.playbackContext?.state ?? 'missing',
    )
  }

  /** Best-effort flush + local ring for desktop diagnostics export. */
  public diagnosticsSnapshot() {
    this.diagnostics.flush()
    return this.diagnostics.snapshot()
  }

  private emitPlaybackSummary(
    outcome: 'render_completed' | 'force_stopped' | 'resume_failed' | 'decode_failed',
  ): void {
    const stats = this.playbackTurnStats
    if (!stats) return
    const now = performance.now()
    this.diagnostics.record('playback_summary', {
      severity: outcome === 'render_completed' ? 'info' : 'warning',
      turnId: stats.turnId ?? undefined,
      metadata: playbackSummaryMetadata(
        stats,
        outcome,
        now,
        this.playbackContext?.state ?? stats.contextState,
      ),
    })
    this.playbackTurnStats = null
  }

  public async preparePlayback(): Promise<boolean> {
    return (await this.ensurePlaybackReady()) !== null
  }

  public async playVoicePreview(base64Data: string, sampleRate = DEFAULT_TTS_SAMPLE_RATE): Promise<void> {
    const context = await this.ensurePlaybackReady()
    if (!context) {
      throw new Error('Audio playback is unavailable on this device.')
    }
    this.stopVoicePreview()
    const rawFloat32 = this.decodePcmF32leBase64(base64Data)
    const audioBuffer = context.createBuffer(1, rawFloat32.length, sampleRate)
    audioBuffer.getChannelData(0).set(rawFloat32)
    const source = context.createBufferSource()
    source.buffer = audioBuffer
    source.connect(context.destination)
    this.previewSource = source
    source.onended = () => {
      if (this.previewSource === source) this.previewSource = null
    }
    source.start()
  }

  private stopVoicePreview(): void {
    if (!this.previewSource) return
    try {
      this.previewSource.stop()
    } catch {
      // already stopped
    }
    this.previewSource = null
  }

  public playAudio(base64Data: string, sampleRate = DEFAULT_TTS_SAMPLE_RATE, turnId?: string): void {
    if (!this.playbackTurnStats || this.playbackTurnStats.turnId !== (turnId ?? null)) {
      this.notePlaybackReceive(turnId)
    }
    this.duckNotificationSound()
    this.blockPhoneMicForTts()
    this.audioQueue.push({ data: base64Data, sampleRate, turnId })
    void this.processQueue()
  }

  public stopPlayback(notifyBackend: boolean = true, notifyPlaybackEnd: boolean = true): void {
    this.stopVoicePreview()
    if (notifyBackend) {
      this.sendMessage('system.stop', {})
    }
    this.interruptBackgroundAudio()

    const hadAudio = this.activeSources.length > 0 || this.audioQueue.length > 0
    this.audioQueue.length = 0
    this.nextStartTime = 0
    
    // Clear activeSources BEFORE stopping — onended checks membership to detect force-stop
    const sourcesToStop = [...this.activeSources]
    this.activeSources.length = 0
    
    sourcesToStop.forEach(source => {
      try { source.stop() } catch (e) { /* ignore */ }
    })
    
    const store = useJarvisStore.getState()
    store.setIsSpeaking(false)
    // Send exactly one playback_end for the entire batch
    if (hadAudio && notifyPlaybackEnd) {
      this.sendMessage('audio.playback_end', this.playbackEndPayload())
    }
    if (hadAudio) this.emitPlaybackSummary('force_stopped')
    this.activePlaybackTurnId = null
    this.releasePhoneMicAfterTts()
  }

  private async processQueue(): Promise<void> {
    if (this.isPlayingQueue) return
    
    const store = useJarvisStore.getState()
    if (!store.isSpeaking && this.audioQueue.length < this.MIN_BUFFER_CHUNKS) {
      return
    }

    this.isPlayingQueue = true
    try {
      const context = await this.ensurePlaybackReady()
      if (!context) {
        if (this.playbackTurnStats) {
          this.emitPlaybackSummary('resume_failed')
        } else {
          this.diagnostics.record('playback_failed', {
            severity: 'error',
            turnId: this.activePlaybackTurnId ?? undefined,
            metadata: { reason: 'context_unavailable' },
          })
        }
        return
      }

      while (this.audioQueue.length > 0) {
        const nextAudio = this.audioQueue.shift()!
        this.scheduleAudioChunk(context, nextAudio)
      }
      const stats = this.playbackTurnStats
      if (
        stats
        && stats.decodeFailures > 0
        && this.activeSources.length === 0
        && this.audioQueue.length === 0
      ) {
        this.emitPlaybackSummary('decode_failed')
      }
    } finally {
      this.isPlayingQueue = false
    }
  }

  private playbackEndPayload(): Record<string, unknown> {
    return this.activePlaybackTurnId ? { turn_id: this.activePlaybackTurnId } : {}
  }

  private scheduleAudioChunk(context: AudioContext, chunk: QueuedAudioChunk): void {
    let rawFloat32: Float32Array
    try {
      rawFloat32 = this.decodePcmF32leBase64(chunk.data)
    } catch (error) {
      console.warn('Skipping invalid TTS PCM chunk:', error)
      if (this.playbackTurnStats) this.playbackTurnStats.decodeFailures += 1
      return
    }

    if (this.playbackTurnStats && this.playbackTurnStats.firstScheduleAt === null) {
      this.playbackTurnStats.firstScheduleAt = performance.now()
      this.playbackTurnStats.contextState = context.state
    }

    const audioBuffer = context.createBuffer(1, rawFloat32.length, chunk.sampleRate)
    audioBuffer.getChannelData(0).set(rawFloat32)

    const source = context.createBufferSource()
    source.buffer = audioBuffer
    source.connect(context.destination)
    if (chunk.turnId) {
      this.activePlaybackTurnId = chunk.turnId
    }
    
    this.activeSources.push(source)

    const currentTime = context.currentTime
    if (this.nextStartTime < currentTime) {
      this.nextStartTime = currentTime + 0.05
    }
    source.start(this.nextStartTime)
    this.nextStartTime += audioBuffer.duration

    const store = useJarvisStore.getState()
    store.setIsSpeaking(true)
    source.onended = () => {
      const index = this.activeSources.indexOf(source)
      if (index === -1) return // Force-stopped by stopPlayback — it already sent playback_end

      this.activeSources.splice(index, 1)

      // Last chunk finished naturally — send playback_end, backend responds with correct state
      if (this.activeSources.length === 0 && this.audioQueue.length === 0) {
        const s = useJarvisStore.getState()
        s.setIsSpeaking(false)
        this.unduckNotificationSound()
        this.sendMessage('audio.playback_end', this.playbackEndPayload())
        this.emitPlaybackSummary('render_completed')
        this.activePlaybackTurnId = null
        this.releasePhoneMicAfterTts()
      }
    }
  }
}

export const jarvisClient = JarvisClient.getInstance()

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    const jarvisGlobal = globalThis as typeof globalThis & { __jarvisClient?: JarvisClient }
    jarvisClient.disconnect()
    if (jarvisGlobal.__jarvisClient === jarvisClient) {
      delete jarvisGlobal.__jarvisClient
    }
  })
}
