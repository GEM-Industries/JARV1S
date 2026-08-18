import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  ClientDiagnosticsRecorder,
  micCaptureStallReason,
  micFlatlineReason,
  notePlaybackChunk,
  playbackSummaryMetadata,
  sanitizeMetadata,
} from './clientDiagnostics'

describe('sanitizeMetadata', () => {
  it('keeps scalars and truncates strings', () => {
    expect(
      sanitizeMetadata({
        ok: true,
        count: 3,
        label: 'x'.repeat(80),
        nested: { a: 1 },
        bad: Number.NaN,
      }),
    ).toEqual({
      ok: true,
      count: 3,
      label: `${'x'.repeat(63)}…`,
    })
  })

  it('caps metadata key count', () => {
    const input: Record<string, unknown> = {}
    for (let i = 0; i < 20; i += 1) input[`k${i}`] = i
    expect(Object.keys(sanitizeMetadata(input))).toHaveLength(12)
  })
})

describe('ClientDiagnosticsRecorder', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('bounds the ring without treating delivered history as incident loss', () => {
    const recorder = new ClientDiagnosticsRecorder()
    recorder.configure(() => true)
    for (let i = 0; i < 55; i += 1) {
      recorder.record('transport_transition', { metadata: { i } })
    }
    recorder.flush()
    const snap = recorder.snapshot()
    expect(snap.events).toHaveLength(50)
    expect(snap.dropped_count).toBe(0)
    expect(snap.pending_count).toBe(0)
    expect(snap.events[0]?.metadata.i).toBe(5)
  })

  it('retains pending events when send fails and flushes on success', () => {
    const recorder = new ClientDiagnosticsRecorder()
    const sends: unknown[] = []
    let accept = false
    recorder.configure((batch) => {
      sends.push(batch)
      return accept
    })

    recorder.record('mic_acquire', { severity: 'error', metadata: { ok: false } })
    recorder.flush()
    expect(sends).toHaveLength(1)
    expect(recorder.snapshot().pending_count).toBe(1)

    accept = true
    recorder.flush()
    expect(sends).toHaveLength(2)
    expect(recorder.snapshot().pending_count).toBe(0)
  })

  it('bounds pending events while disconnected and reports the loss', () => {
    const recorder = new ClientDiagnosticsRecorder()
    const batches: Array<{ dropped_count: number }> = []
    recorder.configure((batch) => {
      batches.push(batch)
      return false
    })

    for (let i = 0; i < 55; i += 1) {
      recorder.record('transport_transition', { metadata: { i } })
    }

    expect(recorder.snapshot().pending_count).toBe(50)
    recorder.flush()
    expect(batches[batches.length - 1]?.dropped_count).toBe(5)
  })

  it('flushes immediately when the batch is full', () => {
    const recorder = new ClientDiagnosticsRecorder()
    const sends: unknown[] = []
    recorder.configure((batch) => {
      sends.push(batch)
      return true
    })
    for (let i = 0; i < 10; i += 1) {
      recorder.record('mic_flatline', { severity: 'warning', metadata: { i } })
    }
    expect(sends).toHaveLength(1)
    expect(recorder.snapshot().pending_count).toBe(0)
  })

  it('schedules a delayed flush for small batches', () => {
    vi.useFakeTimers()
    const recorder = new ClientDiagnosticsRecorder()
    const sends: unknown[] = []
    recorder.configure((batch) => {
      sends.push(batch)
      return true
    })
    recorder.record('notification_failed', { severity: 'error', metadata: { kind: 'timer' } })
    expect(sends).toHaveLength(0)
    vi.advanceTimersByTime(250)
    expect(sends).toHaveLength(1)
  })

  it('rejects unknown event names', () => {
    const recorder = new ClientDiagnosticsRecorder()
    recorder.record('not_a_real_event' as 'mic_acquire')
    expect(recorder.snapshot().events).toHaveLength(0)
  })

  it('flushes retained events after reconnect-style configure', () => {
    const recorder = new ClientDiagnosticsRecorder()
    let accept = false
    const sends: unknown[] = []
    recorder.configure((batch) => {
      sends.push(batch)
      return accept
    })
    recorder.record('transport_transition', { metadata: { phase: 'closed' } })
    recorder.flush()
    expect(recorder.snapshot().pending_count).toBe(1)

    accept = true
    recorder.record('transport_transition', { metadata: { phase: 'open', recovery: 'reconnect' } })
    recorder.flush()
    expect(sends.length).toBeGreaterThanOrEqual(2)
    expect(recorder.snapshot().pending_count).toBe(0)
  })
})

describe('micFlatlineReason', () => {
  it('emits once per acquisition and rate-limits afterward', () => {
    expect(
      micFlatlineReason({
        emitted: false,
        hasStream: true,
        frames: 0,
        peak: 0,
        peakThreshold: 80,
      }),
    ).toBe('no_frames')
    expect(
      micFlatlineReason({
        emitted: true,
        hasStream: true,
        frames: 0,
        peak: 0,
        peakThreshold: 80,
      }),
    ).toBeNull()
    expect(
      micFlatlineReason({
        emitted: false,
        hasStream: true,
        frames: 12,
        peak: 10,
        peakThreshold: 80,
      }),
    ).toBe('flatline')
    expect(
      micFlatlineReason({
        emitted: false,
        hasStream: true,
        frames: 12,
        peak: 200,
        peakThreshold: 80,
      }),
    ).toBeNull()
  })
})

describe('micCaptureStallReason', () => {
  it('detects dead tracks and late frame stalls while unmuted', () => {
    expect(
      micCaptureStallReason({
        muted: true,
        hasStream: true,
        trackLive: false,
        lastFrameAt: 0,
        now: 5000,
        stallMs: 2500,
      }),
    ).toBeNull()
    expect(
      micCaptureStallReason({
        muted: false,
        hasStream: true,
        trackLive: false,
        lastFrameAt: 1000,
        now: 5000,
        stallMs: 2500,
      }),
    ).toBe('track_dead')
    expect(
      micCaptureStallReason({
        muted: false,
        hasStream: true,
        trackLive: true,
        lastFrameAt: 1000,
        now: 4000,
        stallMs: 2500,
      }),
    ).toBe('no_frames')
    expect(
      micCaptureStallReason({
        muted: false,
        hasStream: true,
        trackLive: true,
        lastFrameAt: 3000,
        now: 4000,
        stallMs: 2500,
      }),
    ).toBeNull()
  })
})

describe('playback aggregation', () => {
  it('aggregates chunks into one summary metadata payload', () => {
    let stats = notePlaybackChunk(null, 'turn-1', 1000, 'running')
    stats = notePlaybackChunk(stats, 'turn-1', 1100, 'running')
    stats = notePlaybackChunk(stats, 'turn-1', 1200, 'running')
    stats = { ...stats, firstScheduleAt: 1050 }
    const meta = playbackSummaryMetadata(stats, 'render_completed', 1500, 'running')
    expect(meta.chunks).toBe(3)
    expect(meta.receive_to_schedule_ms).toBe(50)
    expect(meta.receive_to_end_ms).toBe(500)
    expect(meta.outcome).toBe('render_completed')

    const nextTurn = notePlaybackChunk(stats, 'turn-2', 2000, 'suspended')
    expect(nextTurn.chunkCount).toBe(1)
    expect(nextTurn.turnId).toBe('turn-2')
  })
})
