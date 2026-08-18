import type { AudioProcessingProfile } from '../types'

export const DEFAULT_AUDIO_PROCESSING_PROFILE: AudioProcessingProfile = 'standard'

export function isAudioProcessingProfile(value: string | null | undefined): value is AudioProcessingProfile {
  return value === 'standard' || value === 'call_compatibility'
}

export function resolveAudioProcessingProfile(
  preferred: AudioProcessingProfile,
  automaticCallCompatibility: boolean,
): AudioProcessingProfile {
  return automaticCallCompatibility ? 'call_compatibility' : preferred
}

export function buildMicConstraints(
  profile: AudioProcessingProfile,
  selectedInputId?: string,
): MediaTrackConstraints {
  const processingEnabled = profile === 'standard'
  const audio: MediaTrackConstraints = {
    echoCancellation: processingEnabled,
    noiseSuppression: processingEnabled,
    autoGainControl: processingEnabled,
    channelCount: 1,
  }
  if (selectedInputId) {
    audio.deviceId = { exact: selectedInputId }
  }
  return audio
}

export function readAppliedEchoCancellation(settings: MediaTrackSettings): boolean | null {
  return (
    typeof settings.echoCancellation === 'boolean' ? settings.echoCancellation : null
  )
}
