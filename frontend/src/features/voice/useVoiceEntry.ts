import { useCallback, useMemo, useState } from 'react'
import { jarvisClient } from '../../client/JarvisClient'
import { voiceApi } from '../../client/voiceApi'
import { useJarvisStore } from '../../store/useJarvisStore'
import {
  prepareVoiceEntry,
  resolveVoiceControlPresentation,
  type VoiceEntryIssue,
} from './voiceEntry'

const deps = {
  getConfig: () => voiceApi.getConfig(),
  getInputStatus: voiceApi.getInputStatus,
  prepareInput: voiceApi.prepareInput,
  getOutputStatus: voiceApi.getOutputStatus,
  updateConfig: voiceApi.updateConfig,
}

/**
 * Chat-footer voice start. Store owns mic ready/mute; this hook only holds
 * transient prepare progress and the last setup issue.
 */
export function useVoiceEntry() {
  const connectionState = useJarvisStore((s) => s.connectionState)
  const isAudioContextReady = useJarvisStore((s) => s.isAudioContextReady)
  const isMuted = useJarvisStore((s) => s.isMuted)
  const openOverlay = useJarvisStore((s) => s.openOverlay)

  const [preparing, setPreparing] = useState(false)
  const [issue, setIssue] = useState<VoiceEntryIssue | null>(null)

  const connected = connectionState === 'connected'

  const presentation = useMemo(
    () => resolveVoiceControlPresentation({
      connected,
      isAudioContextReady,
      isMuted,
      preparing,
      issue,
    }),
    [connected, isAudioContextReady, isMuted, issue, preparing],
  )

  const openVoiceSettings = useCallback(() => {
    openOverlay('settings', { settingsSection: 'audio' })
  }, [openOverlay])

  const startVoice = useCallback(async () => {
    if (!connected || preparing) return

    if (isAudioContextReady) {
      jarvisClient.toggleMute()
      setIssue(null)
      return
    }

    setPreparing(true)
    setIssue(null)

    try {
      const prepared = await prepareVoiceEntry(deps)
      if (!prepared.ok) {
        setIssue(prepared.issue ?? { kind: 'error', detail: prepared.detail })
        return
      }

      const audio = await jarvisClient.initAudio()
      if (!audio.ok) {
        setIssue({
          kind: 'error',
          detail: audio.error
            ? `${audio.error} Allow microphone access in System Settings, then try again.`
            : 'Microphone access was denied. You can keep typing, or allow access in System Settings.',
        })
        return
      }

      if (prepared.pendingLocalTts) {
        try {
          await voiceApi.updateConfig({
            tts_provider: 'local',
            local_voice_id: prepared.config?.local_voice_id || 'af_heart',
          })
        } catch {
          // Spoken replies are optional; listening still works with text replies.
        }
      }

      if (useJarvisStore.getState().isMuted) {
        jarvisClient.toggleMute()
      }
      setIssue(null)
    } finally {
      setPreparing(false)
    }
  }, [connected, isAudioContextReady, preparing])

  const onPrimaryAction = useCallback(() => {
    if (presentation.action === 'setup') {
      openVoiceSettings()
      return
    }
    void startVoice()
  }, [openVoiceSettings, presentation.action, startVoice])

  return {
    presentation,
    openVoiceSettings,
    onPrimaryAction,
  }
}
