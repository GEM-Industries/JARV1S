/**
 * Bounded client diagnostic breadcrumbs for connection/audio incidents.
 * No audio, transcripts, device IDs, URLs, or stacks.
 */

export const CLIENT_DIAGNOSTIC_EVENTS = [
  'transport_transition',
  'mic_acquire',
  'mic_interrupted',
  'mic_flatline',
  'playback_summary',
  'playback_failed',
  'notification_failed',
  'location_unavailable',
] as const

export type ClientDiagnosticEventName = (typeof CLIENT_DIAGNOSTIC_EVENTS)[number]

export type ClientDiagnosticSeverity = 'info' | 'warning' | 'error'

export type ClientDiagnosticCategory = 'transport' | 'mic' | 'playback' | 'notification' | 'location'

export type ClientDiagnosticScalar = string | number | boolean | null

export interface ClientDiagnosticEvent {
  seq: number
  ts: string
  category: ClientDiagnosticCategory
  event: ClientDiagnosticEventName
  severity: ClientDiagnosticSeverity
  turn_id?: string
  message_id?: string
  metadata: Record<string, ClientDiagnosticScalar>
}

export interface ClientDiagnosticBatch {
  events: ClientDiagnosticEvent[]
  dropped_count: number
}

export interface ClientDiagnosticsSnapshot {
  events: ClientDiagnosticEvent[]
  dropped_count: number
  pending_count: number
}

type SendBatch = (batch: ClientDiagnosticBatch) => boolean

const RING_CAPACITY = 50
const BATCH_SIZE = 10
const FLUSH_DELAY_MS = 250
const MAX_METADATA_KEYS = 12
const MAX_STRING_LEN = 64
const MAX_KEY_LEN = 32

const EVENT_CATEGORY: Record<ClientDiagnosticEventName, ClientDiagnosticCategory> = {
  transport_transition: 'transport',
  mic_acquire: 'mic',
  mic_interrupted: 'mic',
  mic_flatline: 'mic',
  playback_summary: 'playback',
  playback_failed: 'playback',
  notification_failed: 'notification',
  location_unavailable: 'location',
}

const ALLOWED_EVENTS = new Set<string>(CLIENT_DIAGNOSTIC_EVENTS)

function isScalar(value: unknown): value is ClientDiagnosticScalar {
  return (
    value === null
    || typeof value === 'string'
    || typeof value === 'number'
    || typeof value === 'boolean'
  )
}

function sanitizeString(value: string): string {
  const cleaned = value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim()
  return cleaned.length > MAX_STRING_LEN ? `${cleaned.slice(0, MAX_STRING_LEN - 1)}…` : cleaned
}

export function sanitizeMetadata(
  input: Record<string, unknown> | undefined,
): Record<string, ClientDiagnosticScalar> {
  if (!input) return {}
  const out: Record<string, ClientDiagnosticScalar> = {}
  let count = 0
  for (const [rawKey, rawValue] of Object.entries(input)) {
    if (count >= MAX_METADATA_KEYS) break
    const key = sanitizeString(rawKey).slice(0, MAX_KEY_LEN)
    if (!key || !isScalar(rawValue)) continue
    if (typeof rawValue === 'string') {
      out[key] = sanitizeString(rawValue)
    } else if (typeof rawValue === 'number') {
      if (!Number.isFinite(rawValue)) continue
      out[key] = Math.round(rawValue * 1000) / 1000
    } else {
      out[key] = rawValue
    }
    count += 1
  }
  return out
}

export type MicFlatlineReason = 'no_frames' | 'flatline'

/** One-shot flatline decision for an acquisition window. Null = healthy or already emitted. */
export function micFlatlineReason(input: {
  emitted: boolean
  hasStream: boolean
  frames: number
  peak: number
  peakThreshold: number
}): MicFlatlineReason | null {
  if (input.emitted || !input.hasStream) return null
  if (input.frames === 0) return 'no_frames'
  if (input.peak < input.peakThreshold) return 'flatline'
  return null
}

export type MicStallReason = 'track_dead' | 'no_frames'

/** Ongoing capture stall while unmuted: track ended or PCM stopped after flowing. */
export function micCaptureStallReason(input: {
  muted: boolean
  hasStream: boolean
  trackLive: boolean
  lastFrameAt: number | null
  now: number
  stallMs: number
}): MicStallReason | null {
  if (input.muted || !input.hasStream) return null
  if (!input.trackLive) return 'track_dead'
  if (input.lastFrameAt == null) return null
  if (input.now - input.lastFrameAt >= input.stallMs) return 'no_frames'
  return null
}

export interface PlaybackTurnStats {
  turnId: string | null
  firstReceiveAt: number
  firstScheduleAt: number | null
  chunkCount: number
  decodeFailures: number
  contextState: string
}

/** Aggregate TTS chunks into one turn-scoped playback summary. */
export function notePlaybackChunk(
  current: PlaybackTurnStats | null,
  turnId: string | null,
  now: number,
  contextState: string,
): PlaybackTurnStats {
  if (!current || current.turnId !== turnId) {
    return {
      turnId,
      firstReceiveAt: now,
      firstScheduleAt: null,
      chunkCount: 1,
      decodeFailures: 0,
      contextState,
    }
  }
  return { ...current, chunkCount: current.chunkCount + 1 }
}

export function playbackSummaryMetadata(
  stats: PlaybackTurnStats,
  outcome: 'render_completed' | 'force_stopped' | 'resume_failed' | 'decode_failed',
  now: number,
  contextState: string,
): Record<string, ClientDiagnosticScalar> {
  return {
    outcome,
    chunks: stats.chunkCount,
    decode_failures: stats.decodeFailures,
    receive_to_schedule_ms:
      stats.firstScheduleAt === null
        ? null
        : Math.round(stats.firstScheduleAt - stats.firstReceiveAt),
    receive_to_end_ms: Math.round(now - stats.firstReceiveAt),
    context_state: contextState,
  }
}

export class ClientDiagnosticsRecorder {
  private readonly ring: ClientDiagnosticEvent[] = []
  private pending: ClientDiagnosticEvent[] = []
  private seq = 0
  private pendingDroppedCount = 0
  private flushTimer: ReturnType<typeof setTimeout> | null = null
  private sendBatch: SendBatch | null = null

  configure(sendBatch: SendBatch): void {
    this.sendBatch = sendBatch
  }

  record(
    event: ClientDiagnosticEventName,
    options: {
      severity?: ClientDiagnosticSeverity
      turnId?: string
      messageId?: string
      metadata?: Record<string, unknown>
    } = {},
  ): void {
    if (!ALLOWED_EVENTS.has(event)) return

    const entry: ClientDiagnosticEvent = {
      seq: ++this.seq,
      ts: new Date().toISOString(),
      category: EVENT_CATEGORY[event],
      event,
      severity: options.severity ?? 'info',
      metadata: sanitizeMetadata(options.metadata),
    }
    if (options.turnId) entry.turn_id = sanitizeString(options.turnId)
    if (options.messageId) entry.message_id = sanitizeString(options.messageId)

    this.pushRing(entry)
    if (this.pending.length >= RING_CAPACITY) {
      this.pending.shift()
      this.pendingDroppedCount += 1
    }
    this.pending.push(entry)
    if (this.pending.length >= BATCH_SIZE) {
      this.flush()
      return
    }
    this.scheduleFlush()
  }

  /** Flush immediately (e.g. after reconnect). */
  flush(): void {
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
    if (!this.sendBatch || this.pending.length === 0) return

    while (this.pending.length > 0) {
      const chunk = this.pending.slice(0, BATCH_SIZE)
      const batch: ClientDiagnosticBatch = {
        events: chunk,
        dropped_count: this.pendingDroppedCount,
      }
      const ok = this.sendBatch(batch)
      if (!ok) return
      this.pending = this.pending.slice(chunk.length)
      this.pendingDroppedCount = 0
    }
  }

  snapshot(): ClientDiagnosticsSnapshot {
    return {
      events: this.ring.map((event) => ({
        ...event,
        metadata: { ...event.metadata },
      })),
      // Unsent loss only — ring eviction of already-delivered events is not incident loss.
      dropped_count: this.pendingDroppedCount,
      pending_count: this.pending.length,
    }
  }

  /** Test helper. */
  reset(): void {
    this.ring.length = 0
    this.pending = []
    this.seq = 0
    this.pendingDroppedCount = 0
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer)
      this.flushTimer = null
    }
  }

  private pushRing(entry: ClientDiagnosticEvent): void {
    if (this.ring.length >= RING_CAPACITY) {
      this.ring.shift()
    }
    this.ring.push(entry)
  }

  private scheduleFlush(): void {
    if (this.flushTimer !== null) return
    this.flushTimer = setTimeout(() => {
      this.flushTimer = null
      this.flush()
    }, FLUSH_DELAY_MS)
  }
}

const globalKey = '__jarvisClientDiagnostics'

export function getClientDiagnosticsRecorder(): ClientDiagnosticsRecorder {
  const g = globalThis as typeof globalThis & { [globalKey]?: ClientDiagnosticsRecorder }
  if (!g[globalKey]) {
    g[globalKey] = new ClientDiagnosticsRecorder()
  }
  return g[globalKey]
}
