import type {
  AgentState,
  ConnectionState,
  TranscriptItem,
  UIEnvelope,
} from '../../../types'
import type { HostState, LiveAssistantPreview } from '../../../store/useJarvisStore'
import {
  isPassiveDetailWidget,
  isReceiptRailWidget,
} from '../widgets/widgetRail'

export type LiveStagePhase =
  | 'recovery'
  | 'idle'
  | 'detected'
  | 'listening'
  | 'transcribing'
  | 'thinking'
  | 'executing'
  | 'speaking'

export type LiveStageFocalKind =
  | 'empty'
  | 'onboarding'
  | 'projection'
  | 'widget'
  | 'recovery'

export type LiveStageTone = 'neutral' | 'brand' | 'output' | 'warning' | 'danger'

export interface LiveStageInput {
  hostState: HostState
  connectionState: ConnectionState
  agentState: AgentState
  isSpeaking: boolean
  transcript: TranscriptItem[]
  partialTranscript: string | null
  /** Ephemeral scratch from live WS only — never history. */
  liveAssistantPreview: LiveAssistantPreview | null
  activeWidgetId: string | null
  widgets: UIEnvelope[]
}

export interface LiveStagePresentation {
  phase: LiveStagePhase
  label: string
  detail: string | null
  tone: LiveStageTone
  pulse: boolean
  userPreview: string | null
  assistantPreview: string | null
  assistantResponseKey: string | null
  focalKind: LiveStageFocalKind
  foregroundWidget: UIEnvelope | null
  pinnedSupport: UIEnvelope[]
  attentionReceiptIds: string[]
  settled: boolean
}

const LIVE_CAPTURE_PHASES = new Set<LiveStagePhase>([
  'detected',
  'listening',
  'transcribing',
])

export const isLiveCapturePhase = (phase: LiveStagePhase): boolean =>
  LIVE_CAPTURE_PHASES.has(phase)

const LIVE_WORK_PHASES = new Set<LiveStagePhase>([
  'thinking',
  'executing',
  'speaking',
])

export const isPendingApprovalWidget = (widget: UIEnvelope): boolean =>
  widget.component === 'PendingInputWidget'
  && widget.data?.status === 'pending'

export const isBackgroundApprovalReceipt = (widget: UIEnvelope): boolean =>
  isReceiptRailWidget(widget)
  && widget.data?.attention === 'approval'

export const isHeroEligibleWidget = (widget: UIEnvelope): boolean =>
  widget.pinned
  || (!isReceiptRailWidget(widget) && !isPassiveDetailWidget(widget))

const newestFirst = (a: UIEnvelope, b: UIEnvelope): number =>
  (b.layout?.priority || 0) - (a.layout?.priority || 0)
  || (b.created_at || 0) - (a.created_at || 0)

export const selectPinnedSupport = (
  widgets: UIEnvelope[],
  foregroundId: string | null,
): UIEnvelope[] =>
  widgets
    .filter((widget) => widget.pinned && widget.widget_id !== foregroundId)
    .sort(newestFirst)

export const selectForegroundWidget = (
  widgets: UIEnvelope[],
  activeWidgetId: string | null,
): UIEnvelope | null => {
  const pendingApproval = widgets
    .filter(isPendingApprovalWidget)
    .sort(newestFirst)[0]
  if (pendingApproval) return pendingApproval

  if (activeWidgetId) {
    const active = widgets.find((widget) => widget.widget_id === activeWidgetId)
    if (active) return active
  }

  return widgets
    .filter(isHeroEligibleWidget)
    .sort(newestFirst)[0] ?? null
}

export const resolveLiveStagePhase = (input: {
  hostState: HostState
  connectionState: ConnectionState
  agentState: AgentState
  isSpeaking: boolean
}): LiveStagePhase => {
  const { hostState, connectionState, agentState, isSpeaking } = input

  if (
    hostState === 'offline'
    || connectionState === 'disconnected'
    || connectionState === 'connecting'
    || connectionState === 'reconnecting'
    || connectionState === 'error'
  ) {
    return 'recovery'
  }

  // Rendered audio is authoritative for the visual phase. The backend can
  // advance to tool work or emit a provisional barge-in state while queued
  // speech is still playing locally.
  if (isSpeaking || agentState === 'speaking') {
    return 'speaking'
  }

  switch (agentState) {
    case 'waking':
      return 'detected'
    case 'listening':
      return 'listening'
    case 'transcribing':
      return 'transcribing'
    case 'thinking':
    case 'composing_tool':
      return 'thinking'
    case 'running_tool':
      return 'executing'
    case 'idle':
    default:
      return 'idle'
  }
}

const phaseCopy = (
  phase: LiveStagePhase,
  connectionState: ConnectionState,
  hostState: HostState,
): { label: string; detail: string | null; tone: LiveStageTone; pulse: boolean } => {
  switch (phase) {
    case 'recovery':
      if (hostState === 'offline') {
        return {
          label: 'Host offline',
          detail: 'The backend is unavailable. Retry from the control bar.',
          tone: 'danger',
          pulse: false,
        }
      }
      if (connectionState === 'reconnecting') {
        return {
          label: 'Reconnecting',
          detail: 'Restoring the live session…',
          tone: 'warning',
          pulse: true,
        }
      }
      if (connectionState === 'connecting') {
        return {
          label: 'Connecting',
          detail: 'Opening the live session…',
          tone: 'brand',
          pulse: true,
        }
      }
      if (connectionState === 'error') {
        return {
          label: 'Connection error',
          detail: 'Reconnect from the control bar to continue.',
          tone: 'danger',
          pulse: false,
        }
      }
      return {
        label: 'Disconnected',
        detail: 'Connect to start talking or typing with JARV1S.',
        tone: 'neutral',
        pulse: false,
      }
    case 'detected':
      return { label: 'Detected', detail: null, tone: 'brand', pulse: true }
    case 'listening':
      return { label: 'Listening', detail: null, tone: 'brand', pulse: false }
    case 'transcribing':
      return { label: 'Transcribing', detail: null, tone: 'brand', pulse: true }
    case 'thinking':
      return { label: 'Thinking', detail: null, tone: 'brand', pulse: true }
    case 'executing':
      return { label: 'Executing', detail: null, tone: 'brand', pulse: true }
    case 'speaking':
      return { label: 'Speaking', detail: null, tone: 'output', pulse: true }
    case 'idle':
    default:
      return { label: '', detail: null, tone: 'neutral', pulse: false }
  }
}

const latestPartialUserText = (transcript: TranscriptItem[]): string | null => {
  for (let i = transcript.length - 1; i >= 0; i -= 1) {
    const item = transcript[i]
    if (
      item.sender === 'user'
      && item.type === 'text'
      && item.isPartial
      && item.text?.trim()
    ) {
      return item.text.trim()
    }
  }
  return null
}

const clipPreview = (text: string | null, maxChars = 160): string | null => {
  if (!text) return null
  const normalized = text.replace(/\s+/g, ' ').trim()
  if (normalized.length <= maxChars) return normalized
  return `${normalized.slice(0, maxChars - 1).trimEnd()}…`
}

export const resolveLiveStageFocal = (input: {
  phase: LiveStagePhase
  foregroundWidget: UIEnvelope | null
  hasTranscript: boolean
  hasSettledResult: boolean
  hasPinnedSupport: boolean
}): LiveStageFocalKind => {
  const {
    phase,
    foregroundWidget,
    hasTranscript,
    hasSettledResult,
    hasPinnedSupport,
  } = input

  if (phase === 'recovery') return 'recovery'

  if (isLiveCapturePhase(phase)) return 'projection'

  if (LIVE_WORK_PHASES.has(phase)) {
    if (foregroundWidget && isPendingApprovalWidget(foregroundWidget)) {
      return 'widget'
    }
    if (phase === 'speaking' && foregroundWidget && !isPendingApprovalWidget(foregroundWidget)) {
      return 'widget'
    }
    if (phase === 'thinking' || phase === 'executing') {
      return 'projection'
    }
    return foregroundWidget ? 'widget' : 'projection'
  }

  if (foregroundWidget) return 'widget'
  if (hasSettledResult) return 'projection'
  if (!hasTranscript && !hasPinnedSupport) return 'onboarding'
  return 'projection'
}

export const deriveLiveStagePresentation = (
  input: LiveStageInput,
): LiveStagePresentation => {
  const phase = resolveLiveStagePhase({
    hostState: input.hostState,
    connectionState: input.connectionState,
    agentState: input.agentState,
    isSpeaking: input.isSpeaking,
  })

  const copy = phaseCopy(phase, input.connectionState, input.hostState)
  const foregroundWidget = selectForegroundWidget(input.widgets, input.activeWidgetId)
  const pinnedSupport = selectPinnedSupport(input.widgets, foregroundWidget?.widget_id ?? null)
  const hasTranscript = input.transcript.some((item) => item.type === 'text' || item.type === 'code')

  const scratchText = input.liveAssistantPreview?.text?.trim() || null
  const scratchKey = input.liveAssistantPreview?.key ?? null
  const settled = phase === 'idle' && Boolean(scratchText)

  // Live user text only: floating partial or in-flight STT rows — not committed history.
  const userPreview = (isLiveCapturePhase(phase) || LIVE_WORK_PHASES.has(phase))
    ? clipPreview(input.partialTranscript ?? latestPartialUserText(input.transcript))
    : null

  // Scratch only. speech.start clears it, so Listening never bridges a prior turn.
  // Retain while idle/settled or ACTIVE_IDLE listening until dwell/speech clears it.
  const showAssistantPreview = Boolean(scratchText) && (
    phase === 'speaking'
    || settled
    || (isLiveCapturePhase(phase) && !userPreview)
  )
  const assistantPreview = showAssistantPreview ? clipPreview(scratchText) : null
  const assistantResponseKey = scratchKey

  const focalKind = resolveLiveStageFocal({
    phase,
    foregroundWidget,
    hasTranscript,
    hasSettledResult: settled,
    hasPinnedSupport: pinnedSupport.length > 0,
  })

  let detail = copy.detail
  if (
    (phase === 'thinking' || phase === 'executing')
    && foregroundWidget
    && isPendingApprovalWidget(foregroundWidget)
  ) {
    detail = 'Waiting for approval'
  } else if (phase === 'executing' && foregroundWidget?.title) {
    detail = String(foregroundWidget.title)
  }

  return {
    phase,
    label: copy.label,
    detail,
    tone: copy.tone,
    pulse: copy.pulse,
    userPreview,
    assistantPreview,
    assistantResponseKey,
    focalKind,
    // Keep selection available even when projection is focused so support/receipt
    // highlighting stays consistent with the chosen subject.
    foregroundWidget,
    pinnedSupport,
    attentionReceiptIds: input.widgets
      .filter(isBackgroundApprovalReceipt)
      .map((widget) => widget.widget_id),
    settled,
  }
}

/** Visible focal subject for PrimaryCanvas rendering. */
export const resolveVisibleForegroundWidget = (
  presentation: LiveStagePresentation,
): UIEnvelope | null =>
  presentation.focalKind === 'widget' ? presentation.foregroundWidget : null
