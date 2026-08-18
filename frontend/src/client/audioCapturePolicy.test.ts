import { describe, expect, it } from 'vitest'
import {
  buildMicConstraints,
  DEFAULT_AUDIO_PROCESSING_PROFILE,
  isAudioProcessingProfile,
  readAppliedEchoCancellation,
  resolveAudioProcessingProfile,
} from './audioCapturePolicy'

describe('audioCapturePolicy', () => {
  it('defaults to standard profile', () => {
    expect(DEFAULT_AUDIO_PROCESSING_PROFILE).toBe('standard')
    expect(isAudioProcessingProfile('standard')).toBe(true)
    expect(isAudioProcessingProfile('call_compatibility')).toBe(true)
    expect(isAudioProcessingProfile('invalid')).toBe(false)
  })

  it('builds standard constraints with processing enabled', () => {
    expect(buildMicConstraints('standard')).toEqual({
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    })
  })

  it('temporarily overrides the preferred profile during a call', () => {
    expect(resolveAudioProcessingProfile('standard', true)).toBe('call_compatibility')
    expect(resolveAudioProcessingProfile('standard', false)).toBe('standard')
    expect(resolveAudioProcessingProfile('call_compatibility', false)).toBe('call_compatibility')
  })

  it('builds call compatibility constraints with processing disabled', () => {
    expect(buildMicConstraints('call_compatibility')).toEqual({
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
      channelCount: 1,
    })
  })

  it('pins selected input device when provided', () => {
    expect(buildMicConstraints('standard', 'mic-123')).toEqual({
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
      deviceId: { exact: 'mic-123' },
    })
  })

  it('reads applied echo cancellation from track settings', () => {
    expect(readAppliedEchoCancellation({ echoCancellation: true })).toBe(true)
    expect(readAppliedEchoCancellation({ echoCancellation: false })).toBe(false)
  })

  it('returns null when echo cancellation is unavailable', () => {
    expect(readAppliedEchoCancellation({})).toBeNull()
  })
})
