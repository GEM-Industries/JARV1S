import React, { useEffect, useState } from 'react'
import { MicrophoneIcon, SpinnerIcon } from '@phosphor-icons/react'
import { jarvisClient } from '../../../client/JarvisClient'
import { VoiceApiError, voiceApi, type WakeCheckStatus } from '../../../client/voiceApi'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { Button } from '../../ui/Button'
import { PanelSection } from '../../ui/PanelSection'

const WAKE_CHECK_DURATION_MS = 2500

type Phase = 'idle' | 'recording' | 'checking'

function resultCopy(status: WakeCheckStatus): string {
  switch (status) {
    case 'recognized':
      return 'Heard “Jarvis”. No conversation was started.'
    case 'speaker_mismatch':
      return 'Heard “Jarvis”, but it did not match your enrolled voice. Re-record your voice profile if needed.'
    case 'not_detected':
      return 'Nothing detected. That is okay — try again closer to the microphone.'
  }
}

export const WakePhraseCheckCard: React.FC = () => {
  const audioError = useJarvisStore((state) => state.audioDevices.error)
  const muted = useJarvisStore((state) => state.isMuted)
  const [phase, setPhase] = useState<Phase>('idle')
  const [message, setMessage] = useState<string | null>(null)
  const [messageTone, setMessageTone] = useState<'neutral' | 'success' | 'warning' | 'error'>('neutral')

  useEffect(() => {
    return () => jarvisClient.cancelPcmClipCapture()
  }, [])

  const busy = phase !== 'idle'

  const runCheck = async () => {
    if (busy) return
    setPhase('recording')
    setMessage(
      muted
        ? 'Recording briefly for this check. Your mic stays muted afterward.'
        : 'Listening for “Jarvis”…',
    )
    setMessageTone('neutral')
    try {
      const clip = await jarvisClient.capturePcmClip(WAKE_CHECK_DURATION_MS)
      if (!clip.ok) {
        throw new Error(clip.error)
      }
      setPhase('checking')
      setMessage('Checking on this JARV1S host…')
      const result = await voiceApi.checkWakePhrase(clip.value)
      setMessage(resultCopy(result.status))
      setMessageTone(
        result.status === 'recognized'
          ? 'success'
          : result.status === 'speaker_mismatch'
            ? 'warning'
            : 'neutral',
      )
    } catch (err) {
      jarvisClient.cancelPcmClipCapture()
      const detail =
        err instanceof VoiceApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Could not check the wake phrase.'
      setMessage(detail)
      setMessageTone('error')
    } finally {
      setPhase('idle')
    }
  }

  const messageClass =
    messageTone === 'success'
      ? 'text-status-success'
      : messageTone === 'warning'
        ? 'text-status-warning'
        : messageTone === 'error'
          ? 'text-status-danger'
          : 'text-foreground-muted'

  return (
    <PanelSection className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="type-heading text-foreground">Try the wake phrase</h3>
          <p className="mt-1 type-body text-foreground-muted">
            Optional check. JARV1S records a short clip, checks it on this host, then discards it.
            No conversation starts.
          </p>
        </div>
        {busy && <SpinnerIcon className="mt-1 animate-spin text-status-success" size={18} aria-hidden />}
      </div>
      <Button
        className="mt-4"
        size="sm"
        disabled={busy || Boolean(audioError)}
        icon={<MicrophoneIcon size={16} />}
        onClick={() => void runCheck()}
      >
        {phase === 'recording' ? 'Listening…' : phase === 'checking' ? 'Checking…' : 'Test “Jarvis”'}
      </Button>
      {audioError && (
        <p className="mt-3 type-body text-status-danger" role="alert">
          {audioError}
        </p>
      )}
      {message && (
        <p className={`mt-3 type-body ${messageClass}`} role="status">
          {message}
        </p>
      )}
    </PanelSection>
  )
}
