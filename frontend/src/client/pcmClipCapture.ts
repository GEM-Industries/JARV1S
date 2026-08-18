/**
 * Exclusive bounded PCM16 capture for setup flows (wake check, speaker enrollment).
 * While active, chunks are consumed locally and must not be forwarded to live audio.
 */

export type PcmClipCaptureResult =
  | { ok: true; pcm: Uint8Array }
  | { ok: false; error: string }

export interface PcmClipCaptureOptions {
  durationMs: number
  sampleRate: number
  /** Extra grace after durationMs before finishing with whatever was captured. */
  timeoutGraceMs?: number
  setTimeoutFn?: (fn: () => void, ms: number) => number
  clearTimeoutFn?: (id: number) => void
}

export class PcmClipCapture {
  private buffer: Int16Array
  private capturedSamples = 0
  private readonly targetSamples: number
  private timer: number | null = null
  private resolve: ((result: PcmClipCaptureResult) => void) | null = null
  private recording = false
  private settled = false
  private readonly durationMs: number
  private readonly timeoutGraceMs: number
  private readonly setTimeoutFn: (fn: () => void, ms: number) => number
  private readonly clearTimeoutFn: (id: number) => void

  private constructor(
    options: Required<Pick<PcmClipCaptureOptions, 'durationMs' | 'sampleRate'>> &
      Pick<PcmClipCaptureOptions, 'timeoutGraceMs' | 'setTimeoutFn' | 'clearTimeoutFn'>,
    resolve: (result: PcmClipCaptureResult) => void,
  ) {
    this.durationMs = options.durationMs
    this.timeoutGraceMs = options.timeoutGraceMs ?? 250
    this.targetSamples = Math.max(1, Math.round(options.sampleRate * (options.durationMs / 1000)))
    this.buffer = new Int16Array(this.targetSamples)
    this.resolve = resolve
    this.setTimeoutFn = options.setTimeoutFn ?? ((fn, ms) => window.setTimeout(fn, ms))
    this.clearTimeoutFn = options.clearTimeoutFn ?? ((id) => window.clearTimeout(id))
  }

  static create(options: PcmClipCaptureOptions): {
    capture: PcmClipCapture
    done: Promise<PcmClipCaptureResult>
  } {
    if (options.durationMs <= 0) {
      throw new Error('Capture duration must be positive.')
    }
    if (options.sampleRate <= 0) {
      throw new Error('Sample rate must be positive.')
    }
    let resolveDone!: (result: PcmClipCaptureResult) => void
    const done = new Promise<PcmClipCaptureResult>((resolve) => {
      resolveDone = resolve
    })
    const capture = new PcmClipCapture(options, resolveDone)
    return { capture, done }
  }

  /** Start retaining PCM after microphone setup has completed. */
  start(): void {
    if (this.settled) {
      throw new Error('Recording is no longer active.')
    }
    if (this.recording) return
    this.recording = true
    this.capturedSamples = 0
    this.timer = this.setTimeoutFn(
      () => this.finish(),
      this.durationMs + this.timeoutGraceMs,
    )
  }

  get active(): boolean {
    return !this.settled
  }

  /** Push a worklet PCM chunk. Returns true if the capture consumed it exclusively. */
  push(chunk: Int16Array): boolean {
    if (this.settled) return false
    if (!this.recording) return true

    const remaining = this.targetSamples - this.capturedSamples
    const retainedSamples = Math.min(chunk.length, remaining)
    this.buffer.set(chunk.subarray(0, retainedSamples), this.capturedSamples)
    this.capturedSamples += retainedSamples
    if (this.capturedSamples >= this.targetSamples) {
      this.finish()
    }
    return true
  }

  cancel(reason = 'Recording cancelled.'): void {
    if (this.settled) return
    this.settle({ ok: false, error: reason })
  }

  private finish(): void {
    if (this.settled) return
    if (this.capturedSamples === 0) {
      this.settle({ ok: false, error: 'No microphone audio was captured.' })
      return
    }
    const pcm = new Uint8Array(
      this.buffer.buffer.slice(0, this.capturedSamples * Int16Array.BYTES_PER_ELEMENT),
    )
    this.settle({ ok: true, pcm })
  }

  private settle(result: PcmClipCaptureResult): void {
    if (this.settled) return
    this.settled = true
    if (this.timer !== null) {
      this.clearTimeoutFn(this.timer)
      this.timer = null
    }
    this.buffer = new Int16Array(0)
    this.capturedSamples = 0
    const resolve = this.resolve
    this.resolve = null
    resolve?.(result)
  }
}
