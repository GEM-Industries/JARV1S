import React, { useState } from 'react'
import { MicrophoneIcon, SpinnerIcon } from '@phosphor-icons/react'
import {
  VoiceApiError,
  voiceApi,
  type SpeakerProfileStatus,
} from '../../../client/voiceApi'
import { Button } from '../../ui/Button'

function sampleErrorCopy(reason: string | null, fallback: string): string {
  switch (reason) {
    case 'not_enrolled':
      return 'Teach JARV1S your voice first, then try again.'
    case 'node_offline':
      return 'That speaker is not connected.'
    case 'capture_timeout':
      return 'Did not hear enough audio. Say “Jarvis” toward this speaker and try again.'
    case 'too_short':
      return 'That was too short. Say “Jarvis” clearly toward this speaker.'
    case 'too_quiet':
      return 'That was too quiet. Stand closer to the speaker and try again.'
    default:
      return fallback
  }
}

export const RoomSpeakerVoiceSample: React.FC<{
  nodeId: string
  speakerName?: string
  profile: SpeakerProfileStatus | null
  onCaptured?: (status: SpeakerProfileStatus) => void
}> = ({ nodeId, speakerName, profile, onCaptured }) => {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (profile?.node_ids?.includes(nodeId)) return null

  const enrolled = profile?.status === 'enrolled'
  const label = speakerName || 'this speaker'

  const listen = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const status = await voiceApi.captureNodeSpeakerSample(nodeId)
      onCaptured?.(status)
    } catch (err) {
      const reason = err instanceof VoiceApiError ? err.reason : null
      const fallback = err instanceof Error ? err.message : 'Could not save that sample.'
      setError(sampleErrorCopy(reason, fallback))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-3 overflow-hidden rounded-control bg-canvas/30 px-3 py-3">
      <div>
        <p className="type-label text-foreground">Say “Jarvis” toward {label}</p>
        <p className="mt-0.5 type-meta text-foreground-muted">
          {enrolled
            ? 'One sample from this microphone, so JARV1S recognizes you in the room.'
            : 'Teach JARV1S your voice in Settings first, then come back for this step.'}
        </p>
      </div>
      <Button
        size="sm"
        color="brand"
        className="self-start"
        disabled={busy || !enrolled}
        icon={
          busy ? (
            <SpinnerIcon className="animate-spin" size={14} />
          ) : (
            <MicrophoneIcon size={14} />
          )
        }
        onClick={() => void listen()}
      >
        {busy ? 'Listening…' : 'I’m ready'}
      </Button>
      {error && (
        <p className="type-meta text-status-danger" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
