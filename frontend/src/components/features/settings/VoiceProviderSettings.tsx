import React, { useCallback, useEffect, useRef, useState } from 'react'
import { SpinnerIcon, UploadSimpleIcon, WarningIcon } from '@phosphor-icons/react'
import { jarvisClient } from '../../../client/JarvisClient'
import {
  voiceApi,
  type STTProvider,
  type TTSProvider,
  type VoiceInputStatus,
  type VoiceOutputStatus,
} from '../../../client/voiceApi'
import { LOCAL_TTS_VOICES } from '../../../features/voice/localTtsVoices'
import { cn } from '../../../utils/cn'
import { Button } from '../../ui/Button'
import { FieldControl, Input } from '../../ui/FieldControl'
import { PanelSection } from '../../ui/PanelSection'
import { useVoiceSettings } from './useVoiceSettings'

/** Selectable provider/voice tile. Shared by transcription, spoken replies and voice pickers. */
const ChoiceCard: React.FC<{
  label: string
  description: string
  selected: boolean
  disabled?: boolean
  onSelect: () => void
}> = ({ label, description, selected, disabled, onSelect }) => (
  <button
    type="button"
    disabled={disabled}
    aria-pressed={selected}
    onClick={onSelect}
    className={cn(
      'min-h-14 rounded-control px-4 py-3 text-left transition-colors ui-surface-selectable focus:outline-none',
      selected
        ? 'ui-surface-selected'
        : 'bg-surface/15 hover:bg-surface/25',
      disabled && 'cursor-not-allowed opacity-50',
    )}
  >
    <span className="flex items-center justify-between gap-2">
      <span className="type-label text-foreground">{label}</span>
      {selected && <span className="type-meta text-status-success">Selected</span>}
    </span>
    <span className="mt-1 block type-body text-foreground-muted">{description}</span>
  </button>
)

/** Fetch a helper readiness probe, ignoring results from superseded renders. */
function useProbe<T>(probe: () => Promise<T>, deps: React.DependencyList) {
  const [value, setValue] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const refresh = useCallback((next: T) => setValue(next), [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    probe()
      .then((result) => { if (!cancelled) setValue(result) })
      .catch(() => { if (!cancelled) setValue(null) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  return { value, loading, refresh }
}

interface VoiceProviderSettingsProps {
  onConfigChange?: () => void
}

export const VoiceProviderSettings: React.FC<VoiceProviderSettingsProps> = ({
  onConfigChange,
}) => {
  const voice = useVoiceSettings()
  const [voiceId, setVoiceId] = useState('')
  const [editingCartesiaVoice, setEditingCartesiaVoice] = useState(false)
  const [busy, setBusy] = useState<'stt' | 'tts' | 'voice' | 'clone' | 'prepare' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const appleAutoPrepared = useRef(false)
  const ttsProvider = voice.config?.tts_provider ?? 'off'

  const input = useProbe<VoiceInputStatus>(() => voiceApi.getInputStatus('apple_speech'), [])
  const output = useProbe<VoiceOutputStatus>(() => voiceApi.getOutputStatus('local'), [ttsProvider])
  const appleStatus = input.value
  const localOutput = output.value

  const cartesiaStored = voice.cartesia?.status === 'stored'
  const appleReady = appleStatus?.ready ?? false
  const appleNeedsDownload = appleStatus !== null && !appleStatus.ready
    && (appleStatus.state === 'needs_assets' || appleStatus.state === 'needs_permission')
  const appleUnavailable = appleStatus !== null && !appleStatus.ready
    && (appleStatus.state === 'unsupported' || appleStatus.state === 'unavailable')
  const downloadingSpeech = busy === 'prepare'
  const localReady = localOutput?.ready ?? false
  const cartesiaVoiceMissing = !voiceId.trim() && !voice.config?.cartesia_voice_id
  const showCartesiaVoiceFields = ttsProvider === 'cartesia' || (cartesiaStored && cartesiaVoiceMissing)
  const hasCartesiaVoice = Boolean(voice.config?.cartesia_voice_id)

  useEffect(() => {
    setVoiceId(voice.config?.cartesia_voice_id ?? '')
  }, [voice.config?.cartesia_voice_id])

  const run = async (kind: NonNullable<typeof busy>, action: () => Promise<void>) => {
    if (busy) return
    setBusy(kind)
    setError(null)
    try {
      await action()
      onConfigChange?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Voice settings could not be updated.')
    } finally {
      setBusy(null)
    }
  }

  const prepareAppleSpeech = async (): Promise<VoiceInputStatus> => {
    const status = await voiceApi.prepareInput()
    input.refresh(status)
    if (!status.ready) {
      throw new Error(status.detail ?? 'Could not prepare on-device speech.')
    }
    return status
  }

  const prepareApple = () => run('prepare', async () => {
    await prepareAppleSpeech()
  })

  useEffect(() => {
    if (appleAutoPrepared.current) return
    if (voice.config?.stt_provider !== 'apple_speech' || !appleNeedsDownload) return
    appleAutoPrepared.current = true
    void prepareApple()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appleNeedsDownload, voice.config?.stt_provider])

  const selectSTT = (provider: STTProvider) => run('stt', async () => {
    await voice.selectSTT(provider)
    if (provider !== 'apple_speech') {
      return
    }
    const status = await voiceApi.getInputStatus('apple_speech', { force: true })
    input.refresh(status)
    if (status.ready) {
      return
    }
    if (status.state === 'needs_assets' || status.state === 'needs_permission') {
      setBusy('prepare')
      await prepareAppleSpeech()
      return
    }
    throw new Error(status.detail ?? 'On this Mac speech is not ready yet.')
  })

  const selectTTS = (provider: TTSProvider) => run('tts', async () => {
    if (provider === 'cartesia') {
      await voice.selectTTS('cartesia', {
        cartesia_voice_id: voiceId.trim() || voice.config?.cartesia_voice_id || null,
      })
      return
    }
    if (provider === 'local') {
      await voice.selectTTS('local', {
        local_voice_id: voice.config?.local_voice_id ?? 'af_heart',
      })
      const status = await voiceApi.getOutputStatus('local')
      output.refresh(status)
      return
    }
    await voice.selectTTS('off')
  })

  const saveCartesiaVoice = () => run('voice', async () => {
    await voice.saveCartesiaVoiceId(voiceId)
    setEditingCartesiaVoice(false)
  })

  const saveLocalVoice = (voiceChoice: string) => run('voice', async () => {
    await jarvisClient.preparePlayback()
    await voice.saveLocalVoiceId(voiceChoice)
    const preview = await voiceApi.previewLocalVoice()
    await jarvisClient.playVoicePreview(preview.audio, preview.sample_rate)
  })

  const clone = (file: File) => run('clone', async () => {
    if (file.size > 10 * 1024 * 1024) throw new Error('Choose an audio clip smaller than 10 MB.')
    await voice.cloneVoice(file)
    setEditingCartesiaVoice(false)
  })

  if (voice.loading) {
    return <div className="flex justify-center py-8 text-foreground-subtle"><SpinnerIcon className="animate-spin" size={18} /></div>
  }

  if (voice.error) {
    return (
      <PanelSection className="p-4">
        <p className="type-body text-status-danger" role="alert">{voice.error}</p>
        <Button className="mt-3" size="sm" onClick={() => void voice.reload()}>Retry</Button>
      </PanelSection>
    )
  }

  return (
    <div className="space-y-4">
      {error && (
        <p className="flex items-start gap-2 type-body text-status-danger" role="alert">
          <WarningIcon size={15} className="mt-0.5 shrink-0" />
          {error}
        </p>
      )}

      <PanelSection className="p-4">
        <h3 className="type-heading text-foreground">Understand me</h3>
        <p className="mt-1 type-body text-foreground-muted">On this Mac is recommended when available. Cartesia is an optional cloud alternative.</p>
        <div className="mt-4 grid gap-2 sm:grid-cols-2">
          {([
            {
              provider: 'apple_speech',
              label: 'On this Mac',
              description: input.loading
                ? 'Checking on-device speech…'
                : appleUnavailable
                  ? (appleStatus?.detail ?? 'Unavailable on this system')
                  : appleReady
                    ? 'Recommended · Apple Speech'
                    : appleNeedsDownload
                      ? 'Needs a one-time download'
                      : (appleStatus?.detail ?? 'Needs setup'),
              blocked: input.loading || appleUnavailable,
            },
            {
              provider: 'cartesia',
              label: 'Cartesia',
              description: cartesiaStored ? 'Cloud understanding' : 'Connect in Connections first',
              blocked: !cartesiaStored,
            },
          ] as const).map(({ provider, label, description, blocked }) => (
            <ChoiceCard
              key={provider}
              label={label}
              description={description}
              selected={voice.config?.stt_provider === provider}
              disabled={busy !== null || blocked}
              onSelect={() => void selectSTT(provider)}
            />
          ))}
        </div>
        {voice.config?.stt_provider === 'apple_speech' && downloadingSpeech && (
          <div className="mt-4 space-y-2 rounded-control border border-outline/20 bg-canvas-sunken/20 px-4 py-3">
            <p className="type-meta text-foreground-muted">
              Downloading on-device speech…
            </p>
            <div className="space-y-1">
              <div className="h-1.5 overflow-hidden rounded-full bg-surface/40">
                <div
                  className="h-full w-[4%] animate-pulse rounded-full bg-brand transition-all duration-300"
                />
              </div>
              <p className="type-meta text-foreground-subtle">
                Preparing download…
              </p>
            </div>
          </div>
        )}
      </PanelSection>

      <PanelSection className="p-4">
        <h3 className="type-heading text-foreground">Spoken replies</h3>
        <p className="mt-1 type-body text-foreground-muted">Optional. Text replies always work.</p>
        <div className="mt-4 grid gap-2 sm:grid-cols-3">
          {([
            { provider: 'off', label: 'Off', description: 'Text replies only', blocked: false },
            {
              provider: 'local',
              label: 'On this Mac',
              description: output.loading
                ? 'Checking on-device speech…'
                : localReady
                  ? 'Recommended · Kokoro'
                  : (localOutput?.detail ?? 'Helper unavailable'),
              blocked: output.loading || !localReady,
            },
            {
              provider: 'cartesia',
              label: 'Cartesia',
              description: cartesiaStored
                ? (voice.config?.cartesia_voice_id ? 'Cloud voice' : 'Needs a voice ID')
                : 'Connect in Connections first',
              blocked: !cartesiaStored || cartesiaVoiceMissing,
            },
          ] as const).map(({ provider, label, description, blocked }) => (
            <ChoiceCard
              key={provider}
              label={label}
              description={description}
              selected={ttsProvider === provider}
              disabled={busy !== null || blocked}
              onSelect={() => void selectTTS(provider)}
            />
          ))}
        </div>

        {showCartesiaVoiceFields && (
          <div className="mt-4 border-t border-outline/15 pt-4">
            <input
              ref={fileInputRef}
              type="file"
              accept=".flac,.mp3,.mpeg,.mpga,.oga,.ogg,.wav,.webm,audio/*"
              className="hidden"
              aria-label="Voice clone audio clip"
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void clone(file)
                event.target.value = ''
              }}
            />
            {hasCartesiaVoice && !editingCartesiaVoice ? (
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="type-label text-foreground">Cloud voice configured</p>
                  <p className="mt-1 type-meta text-foreground-subtle">
                    Cartesia will use your saved speaking voice.
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    variant="ghost"
                    color="neutral"
                    disabled={busy !== null}
                    onClick={() => setEditingCartesiaVoice(true)}
                  >
                    Change
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    color="neutral"
                    icon={<UploadSimpleIcon size={15} />}
                    disabled={busy !== null}
                    onClick={() => fileInputRef.current?.click()}
                  >
                    {busy === 'clone' ? 'Cloning…' : 'Clone from clip'}
                  </Button>
                </div>
              </div>
            ) : (
              <div className="space-y-3">
                <FieldControl label="Cartesia voice ID" htmlFor="cartesia-voice-id">
                  <Input
                    id="cartesia-voice-id"
                    value={voiceId}
                    onChange={(event) => setVoiceId(event.target.value)}
                    placeholder="Paste a voice ID or clone from a clip"
                    spellCheck={false}
                    disabled={busy !== null}
                  />
                </FieldControl>
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" disabled={busy !== null || !voiceId.trim()} onClick={() => void saveCartesiaVoice()}>
                    Save voice
                  </Button>
                  {hasCartesiaVoice && (
                    <Button
                      size="sm"
                      variant="ghost"
                      color="neutral"
                      disabled={busy !== null}
                      onClick={() => {
                        setVoiceId(voice.config?.cartesia_voice_id ?? '')
                        setEditingCartesiaVoice(false)
                      }}
                    >
                      Cancel
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    color="neutral"
                    icon={<UploadSimpleIcon size={15} />}
                    disabled={busy !== null || !cartesiaStored}
                    onClick={() => fileInputRef.current?.click()}
                  >
                    {busy === 'clone' ? 'Cloning…' : 'Clone from clip'}
                  </Button>
                </div>
              </div>
            )}
            <p className="mt-3 type-meta text-foreground-subtle">
              Cloning changes JARV1S’s speaking voice. Owner recognition is configured separately.
            </p>
          </div>
        )}

        {ttsProvider === 'local' && (
          <div className="mt-4 grid gap-2 sm:grid-cols-2">
            {LOCAL_TTS_VOICES.map((item) => (
              <ChoiceCard
                key={item.id}
                label={item.label}
                description={item.description}
                selected={voice.config?.local_voice_id === item.id}
                disabled={busy !== null}
                onSelect={() => void saveLocalVoice(item.id)}
              />
            ))}
          </div>
        )}
      </PanelSection>

    </div>
  )
}
