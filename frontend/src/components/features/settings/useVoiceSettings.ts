import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  credentialsApi,
  type CredentialCard,
} from '../../../client/credentialsApi'
import {
  voiceApi,
  type STTProvider,
  type TTSProvider,
  type VoiceRuntimeConfig,
} from '../../../client/voiceApi'

export function useVoiceSettings() {
  const [cartesia, setCartesia] = useState<CredentialCard | null>(null)
  const [config, setConfig] = useState<VoiceRuntimeConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [credentials, voiceConfig] = await Promise.all([
        credentialsApi.list(),
        voiceApi.getConfig(),
      ])
      setCartesia(credentials.items.find((item) => item.id === 'cartesia') ?? null)
      setConfig(voiceConfig)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load voice settings.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const saveCartesia = useCallback(async (apiKey: string) => {
    const result = await credentialsApi.save('cartesia', apiKey)
    setCartesia(result.card)
    // Keep the current STT provider; do not auto-switch to Cartesia on key save.
    const voiceConfig = await voiceApi.getConfig()
    setConfig(voiceConfig)
    return result.message
  }, [])

  const selectSTT = useCallback(async (provider: STTProvider) => {
    const updated = await voiceApi.updateConfig({ stt_provider: provider })
    setConfig(updated)
  }, [])

  const selectTTS = useCallback(async (provider: TTSProvider, extras?: {
    cartesia_voice_id?: string | null
    local_voice_id?: string | null
  }) => {
    const updated = await voiceApi.updateConfig({
      tts_provider: provider,
      ...extras,
    })
    setConfig(updated)
  }, [])

  const saveCartesiaVoiceId = useCallback(async (voiceId: string) => {
    const updated = await voiceApi.updateConfig({
      tts_provider: 'cartesia',
      cartesia_voice_id: voiceId.trim() || null,
    })
    setConfig(updated)
  }, [])

  const saveLocalVoiceId = useCallback(async (voiceId: string) => {
    const updated = await voiceApi.updateConfig({
      tts_provider: 'local',
      local_voice_id: voiceId.trim(),
    })
    setConfig(updated)
  }, [])

  const cloneVoice = useCallback(async (clip: File) => {
    const updated = await voiceApi.cloneVoice(clip)
    setConfig(updated)
  }, [])

  return useMemo(() => ({
    cartesia,
    config,
    loading,
    error,
    reload: load,
    saveCartesia,
    selectSTT,
    selectTTS,
    saveCartesiaVoiceId,
    saveLocalVoiceId,
    cloneVoice,
  }), [
    cartesia,
    cloneVoice,
    config,
    error,
    load,
    loading,
    saveCartesia,
    saveCartesiaVoiceId,
    saveLocalVoiceId,
    selectSTT,
    selectTTS,
  ])
}
