import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { CheckCircleIcon, MicrophoneIcon, SpinnerIcon } from '@phosphor-icons/react'
import { jarvisClient } from '../../../client/JarvisClient'
import {
  VoiceApiError,
  voiceApi,
  type SpeakerProfileStatus,
} from '../../../client/voiceApi'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { Button } from '../../ui/Button'
import { PanelSection } from '../../ui/PanelSection'
import { StatusDot } from '../../ui/StatusDot'

export const REQUIRED_VOICE_SAMPLES = 5

const ENROLLMENT_PROMPTS = [
  { text: 'Jarvis', durationMs: 2200 },
  { text: 'Jarvis', durationMs: 2200 },
  { text: 'Jarvis', durationMs: 2200 },
  { text: "Jarvis, what's the weather today?", durationMs: 4000 },
  { text: 'Turn off the lights, Jarvis.', durationMs: 4000 },
] as const

type Phase = 'loading' | 'idle' | 'recording' | 'processing' | 'enrolled'

export type OwnerVoiceEnrollmentVariant = 'setup' | 'settings'

export interface OwnerVoiceEnrollmentProps {
  variant?: OwnerVoiceEnrollmentVariant
  /** Called after a successful enrollment write (setup uses this to advance). */
  onEnrolled?: (status: SpeakerProfileStatus) => void
  /** Called once profile status is known (setup uses this to skip if already enrolled). */
  onStatus?: (status: SpeakerProfileStatus) => void
  className?: string
}

function reasonCopy(reason: string | null, fallback: string): string {
  switch (reason) {
    case 'too_short':
      return 'That recording was too short. Try the prompt again.'
    case 'too_quiet':
      return 'We could not hear that clearly. Move closer and try again.'
    case 'clipped':
      return 'That recording was too loud. Speak naturally and try again.'
    case 'inconsistent_samples':
      return 'Those samples did not match closely enough. Re-record this one.'
    default:
      return fallback
  }
}

function promptGuidance(prompt: string, muted: boolean): string {
  const base = `Say “${prompt}” naturally.`
  if (muted) {
    return `${base} The microphone is temporarily active for this sample.`
  }
  return `${base} Recording stops automatically.`
}

export const OwnerVoiceEnrollment: React.FC<OwnerVoiceEnrollmentProps> = ({
  variant = 'settings',
  onEnrolled,
  onStatus,
  className,
}) => {
  const audioReady = useJarvisStore((state) => state.isAudioContextReady)
  const audioError = useJarvisStore((state) => state.audioDevices.error)
  const muted = useJarvisStore((state) => state.isMuted)
  const [profile, setProfile] = useState<SpeakerProfileStatus | null>(null)
  const [phase, setPhase] = useState<Phase>('loading')
  const [clips, setClips] = useState<Uint8Array[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const onEnrolledRef = useRef(onEnrolled)
  const onStatusRef = useRef(onStatus)
  onEnrolledRef.current = onEnrolled
  onStatusRef.current = onStatus

  const loadProfile = useCallback(async () => {
    setLoadError(null)
    setPhase('loading')
    try {
      const status = await voiceApi.getSpeakerProfile()
      setProfile(status)
      setPhase(status.status === 'enrolled' ? 'enrolled' : 'idle')
      onStatusRef.current?.(status)
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : 'Could not load voice profile.')
    }
  }, [])

  useEffect(() => {
    void loadProfile()
    return () => jarvisClient.cancelPcmClipCapture()
  }, [loadProfile])

  const sampleIndex = clips.length + 1
  const currentPrompt = ENROLLMENT_PROMPTS[Math.min(clips.length, ENROLLMENT_PROMPTS.length - 1)]
  const statusTone = profile?.status === 'enrolled' ? 'success' : audioReady ? 'warning' : 'off'
  const isSetup = variant === 'setup'

  const title = useMemo(() => {
    if (phase === 'loading') return 'Loading voice profile'
    if (phase === 'recording') {
      return `Recording ${Math.min(sampleIndex, REQUIRED_VOICE_SAMPLES)} of ${REQUIRED_VOICE_SAMPLES}`
    }
    if (phase === 'processing') return 'Saving your voice profile'
    if (phase === 'enrolled') return 'JARV1S recognizes your voice'
    if (clips.length > 0) return `Voice samples: ${clips.length} of ${REQUIRED_VOICE_SAMPLES}`
    return isSetup ? 'Teach JARV1S your voice' : 'Your voice profile'
  }, [clips.length, isSetup, phase, sampleIndex])

  const description = useMemo(() => {
    if (phase === 'loading') {
      return 'Checking for an existing local voice profile.'
    }
    if (phase === 'recording' || (phase === 'idle' && clips.length > 0 && clips.length < REQUIRED_VOICE_SAMPLES)) {
      return promptGuidance(currentPrompt.text, muted)
    }
    if (phase === 'processing') {
      return 'Creating a local voice profile. Recordings are discarded after this step.'
    }
    if (phase === 'enrolled') {
      return isSetup
        ? 'Wake and interrupt checks will prefer your voice on this Mac and room speakers. You can re-record later in Settings.'
        : 'Wake and interrupt checks prefer your voice on this Mac and room speakers. Re-record anytime if recognition drifts.'
    }
    if (clips.length > 0) {
      return profile?.status === 'enrolled'
        ? 'Continue recording. Your current voice profile remains active until the replacement is ready.'
        : promptGuidance(currentPrompt.text, muted)
    }
    return isSetup
      ? 'About a minute. Three short “Jarvis” samples, then two natural requests. The profile is for you — used on this Mac and room speakers. Recordings are discarded after saving.'
      : 'Optional and separate from voice cloning. Three short “Jarvis” samples, then two natural requests. Used on this Mac and room speakers. Until then, the wake word may respond to other voices.'
  }, [clips.length, currentPrompt.text, isSetup, muted, phase, profile?.status])

  const startRecording = async () => {
    if (busy) return
    let submittedClips: Uint8Array[] | null = null
    setBusy(true)
    setError(null)
    setPhase('recording')
    try {
      if (!audioReady) {
        const ready = await jarvisClient.initAudio()
        if (!ready.ok) {
          throw new Error(ready.error)
        }
      }
      const prompt = ENROLLMENT_PROMPTS[clips.length]
      const result = await jarvisClient.capturePcmClip(prompt.durationMs)
      if (!result.ok) {
        throw new Error(result.error)
      }
      const nextClips = [...clips, result.value]
      setClips(nextClips)
      if (nextClips.length < REQUIRED_VOICE_SAMPLES) {
        setPhase('idle')
        return
      }

      setPhase('processing')
      submittedClips = nextClips
      const status = await voiceApi.upsertSpeakerProfile({ clips: nextClips })
      setProfile(status)
      setClips([])
      setPhase('enrolled')
      onEnrolledRef.current?.(status)
      onStatusRef.current?.(status)
    } catch (err) {
      jarvisClient.cancelPcmClipCapture()
      if (err instanceof VoiceApiError) {
        setError(reasonCopy(err.reason, err.message))
        if (submittedClips) {
          const failedIndex =
            err.clipIndex !== null && err.clipIndex >= 1 && err.clipIndex <= submittedClips.length
              ? err.clipIndex - 1
              : submittedClips.length - 1
          setClips(submittedClips.slice(0, failedIndex))
        }
      } else {
        setError(err instanceof Error ? err.message : 'Could not record that sample.')
        if (submittedClips) {
          setClips(submittedClips.slice(0, -1))
        }
      }
      setPhase('idle')
    } finally {
      setBusy(false)
    }
  }

  const resetForRerecord = () => {
    setClips([])
    setError(null)
    setPhase('idle')
  }

  const removeProfile = async () => {
    if (busy || isSetup) return
    setBusy(true)
    setError(null)
    try {
      const status = await voiceApi.deleteSpeakerProfile()
      setProfile(status)
      setClips([])
      setPhase('idle')
      onStatusRef.current?.(status)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove voice profile.')
    } finally {
      setBusy(false)
    }
  }

  if (loadError) {
    return (
      <PanelSection className={className ?? 'p-4'}>
        <p className="type-body text-status-danger" role="alert">
          {loadError}
        </p>
        <Button className="mt-3" size="md" variant="ghost" color="neutral" onClick={() => void loadProfile()}>
          Retry
        </Button>
      </PanelSection>
    )
  }

  return (
    <PanelSection className={className ?? 'p-4'}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <StatusDot status={statusTone} size="md" className="mt-1.5" />
          <div className="min-w-0">
            <h3 className="type-heading text-foreground">{title}</h3>
            <p className="mt-1 type-body text-foreground-muted">{description}</p>
          </div>
        </div>
        {(phase === 'loading' || phase === 'recording' || phase === 'processing') && (
          <SpinnerIcon className="mt-1 animate-spin text-status-success" size={18} aria-hidden />
        )}
        {phase === 'enrolled' && profile?.status === 'enrolled' && clips.length === 0 && (
          <CheckCircleIcon className="mt-1 text-status-success" size={20} aria-hidden />
        )}
      </div>

      {(phase === 'idle' || phase === 'recording') && (
        <div className="mt-4">
          <div
            className="mb-3 flex gap-1"
            aria-label={`Progress ${clips.length} of ${REQUIRED_VOICE_SAMPLES}`}
          >
            {Array.from({ length: REQUIRED_VOICE_SAMPLES }).map((_, index) => (
              <span
                key={index}
                className={`h-1.5 flex-1 rounded-full ${
                  index < clips.length ? 'bg-brand' : 'bg-surface/40'
                }`}
              />
            ))}
          </div>
          {(phase === 'idle' || phase === 'recording') && clips.length < REQUIRED_VOICE_SAMPLES && (
            <p className="mb-3 type-body text-foreground">
              Prompt {sampleIndex}: “{currentPrompt.text}”
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button
              size="md"
              disabled={busy || Boolean(audioError)}
              icon={<MicrophoneIcon size={16} />}
              onClick={() => void startRecording()}
            >
              {clips.length === 0
                ? isSetup
                  ? 'Teach JARV1S your voice'
                  : 'Start'
                : `Record ${sampleIndex} of ${REQUIRED_VOICE_SAMPLES}`}
            </Button>
            {clips.length > 0 && phase === 'idle' && (
              <Button
                size="md"
                variant="ghost"
                color="neutral"
                disabled={busy}
                onClick={() => {
                  setClips((prev) => prev.slice(0, -1))
                  setError(null)
                }}
              >
                Retry last
              </Button>
            )}
          </div>
        </div>
      )}

      {phase === 'enrolled' && !isSetup && (
        <div className="mt-4 flex flex-wrap gap-2">
          <Button size="md" disabled={busy} onClick={resetForRerecord}>
            Re-record
          </Button>
          <Button
            size="md"
            variant="ghost"
            color="neutral"
            disabled={busy}
            onClick={() => void removeProfile()}
          >
            Remove voice profile
          </Button>
        </div>
      )}

      {phase === 'enrolled' && isSetup && (
        <p className="mt-3 type-meta text-foreground-subtle" role="status">
          Voice profile ready for this Mac and room speakers.
        </p>
      )}

      {audioError && (
        <p className="mt-3 type-body text-status-danger" role="alert">
          {audioError}
        </p>
      )}
      {error && (
        <p className="mt-3 type-body text-status-danger" role="alert">
          {error}
        </p>
      )}
    </PanelSection>
  )
}
