import { describe, expect, it, vi } from 'vitest'
import { PcmClipCapture } from './pcmClipCapture'

function chunk(samples: number, value = 1): Int16Array {
  return Int16Array.from({ length: samples }, () => value)
}

describe('PcmClipCapture', () => {
  it('aggregates chunks until the target byte count and finishes', async () => {
    const { capture, done } = PcmClipCapture.create({
      durationMs: 1000,
      sampleRate: 16_000,
      setTimeoutFn: () => 0,
      clearTimeoutFn: () => undefined,
    })
    capture.start()

    // 16000 samples/sec * 1s * 2 bytes = 32000 bytes target
    expect(capture.push(chunk(8000))).toBe(true)
    expect(capture.active).toBe(true)
    expect(capture.push(chunk(8000))).toBe(true)

    const result = await done
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.pcm.byteLength).toBe(32_000)
    }
    expect(capture.active).toBe(false)
    expect(capture.push(chunk(16))).toBe(false)
  })

  it('finishes on timeout with whatever was captured', async () => {
    let timeoutFn: (() => void) | undefined
    const { capture, done } = PcmClipCapture.create({
      durationMs: 500,
      sampleRate: 16_000,
      timeoutGraceMs: 0,
      setTimeoutFn: (fn) => {
        timeoutFn = fn
        return 1
      },
      clearTimeoutFn: () => undefined,
    })
    capture.start()

    capture.push(chunk(100, 7))
    expect(timeoutFn).toBeTypeOf('function')
    timeoutFn?.()

    const result = await done
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(result.pcm.byteLength).toBe(200)
      expect(new Int16Array(result.pcm.buffer)[0]).toBe(7)
    }
  })

  it('fails when the microphone produces no audio', async () => {
    let timeoutFn: (() => void) | undefined
    const { capture, done } = PcmClipCapture.create({
      durationMs: 500,
      sampleRate: 16_000,
      setTimeoutFn: (fn) => {
        timeoutFn = fn
        return 1
      },
      clearTimeoutFn: () => undefined,
    })
    capture.start()
    timeoutFn?.()

    await expect(done).resolves.toEqual({
      ok: false,
      error: 'No microphone audio was captured.',
    })
  })

  it('cancels exclusively and rejects further pushes', async () => {
    const clearTimeoutFn = vi.fn()
    const { capture, done } = PcmClipCapture.create({
      durationMs: 2000,
      sampleRate: 16_000,
      setTimeoutFn: () => 42,
      clearTimeoutFn,
    })
    capture.start()

    capture.push(chunk(64))
    capture.cancel('Stopped.')
    expect(capture.push(chunk(64))).toBe(false)

    const result = await done
    expect(result).toEqual({ ok: false, error: 'Stopped.' })
    expect(clearTimeoutFn).toHaveBeenCalledWith(42)
  })

  it('rejects invalid options', () => {
    expect(() => PcmClipCapture.create({ durationMs: 0, sampleRate: 16_000 })).toThrow(
      /duration must be positive/,
    )
    expect(() => PcmClipCapture.create({ durationMs: 1000, sampleRate: 0 })).toThrow(
      /Sample rate must be positive/,
    )
  })

  it('drops setup frames before recording starts and trims the final chunk', async () => {
    const { capture, done } = PcmClipCapture.create({
      durationMs: 1,
      sampleRate: 1000,
      setTimeoutFn: () => 0,
      clearTimeoutFn: () => undefined,
    })

    expect(capture.push(chunk(2, 9))).toBe(true)
    capture.start()
    capture.push(chunk(2, 4))

    const result = await done
    expect(result.ok).toBe(true)
    if (result.ok) {
      expect(Array.from(new Int16Array(result.pcm.buffer))).toEqual([4])
    }
  })
})
