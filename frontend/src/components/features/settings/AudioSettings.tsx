import React, { useEffect, useState } from 'react'
import { ArrowsClockwiseIcon, CaretDownIcon, MicrophoneIcon, SpeakerHighIcon } from '@phosphor-icons/react'
import { jarvisClient } from '../../../client/JarvisClient'
import {
  voiceApi,
  type VoiceInputStatus,
  type VoiceOutputStatus,
  type VoiceRuntimeConfig,
} from '../../../client/voiceApi'
import { useJarvisStore } from '../../../store/useJarvisStore'
import type { AudioProcessingProfile } from '../../../types'
import { summarizeVoicePipeline } from '../../../features/voice/voiceEntry'
import { cn } from '../../../utils/cn'
import { Button } from '../../ui/Button'
import { StatusDot } from '../../ui/StatusDot'
import { Select } from '../../ui/Select'
import { FieldControl } from '../../ui/FieldControl'
import { PanelSection } from '../../ui/PanelSection'
import { Switch } from '../../ui/Switch'
import { VoiceEnrollmentCard } from './VoiceEnrollmentCard'
import { VoiceProviderSettings } from './VoiceProviderSettings'
import { WakePhraseCheckCard } from './WakePhraseCheckCard'

export const AudioSettings: React.FC = () => {
  const devices = useJarvisStore((state) => state.audioDevices)
  const micReady = useJarvisStore((state) => state.isAudioContextReady)
  const muted = useJarvisStore((state) => state.isMuted)
  const toolCues = useJarvisStore((state) => state.preferences.audio.tool_cues_enabled)
  const [busy, setBusy] = useState(false)
  const [testingSpeaker, setTestingSpeaker] = useState(false)
  const [audioFeedback, setAudioFeedback] = useState<{
    tone: 'success' | 'error'
    message: string
  } | null>(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [config, setConfig] = useState<VoiceRuntimeConfig | null>(null)
  const [input, setInput] = useState<VoiceInputStatus | null>(null)
  const [output, setOutput] = useState<VoiceOutputStatus | null>(null)

  const refreshPipeline = async () => {
    try {
      const voiceConfig = await voiceApi.getConfig()
      setConfig(voiceConfig)
      const [inputStatus, outputStatus] = await Promise.all([
        voiceApi.getInputStatus(voiceConfig.stt_provider),
        voiceConfig.tts_provider === 'off'
          ? Promise.resolve(null)
          : voiceApi.getOutputStatus(voiceConfig.tts_provider),
      ])
      setInput(inputStatus)
      setOutput(outputStatus)
    } catch {
      // Keep last known summary; provider panels show their own errors.
    }
  }

  useEffect(() => {
    void jarvisClient.refreshAudioDevices()
    void refreshPipeline()
  }, [])

  const run = async (action: () => Promise<unknown>) => {
    if (busy) return
    setBusy(true)
    try {
      await action()
    } finally {
      setBusy(false)
    }
  }

  const summary = summarizeVoicePipeline({ micReady, muted, config, input, output })
  const selectedInput = devices.inputs.find((device) => device.deviceId === devices.selectedInputId)
  const understanding =
    config?.stt_provider === 'apple_speech'
      ? 'On this Mac'
      : config?.stt_provider === 'cartesia'
        ? 'Cartesia cloud'
        : 'Checking…'
  const replies =
    config?.tts_provider === 'local'
      ? 'Kokoro on this Mac'
      : config?.tts_provider === 'cartesia'
        ? 'Cartesia cloud'
        : config?.tts_provider === 'off'
          ? 'Text only'
          : 'Checking…'

  const enableMicrophone = async () => {
    if (busy) return
    setBusy(true)
    setAudioFeedback(null)
    try {
      const result = await jarvisClient.initAudio()
      setAudioFeedback({
        tone: result.ok ? 'success' : 'error',
        message: result.ok
          ? 'Microphone is ready. Audio choices stay on this device.'
          : `${result.error} Allow microphone access in System Settings, then try again.`,
      })
      await refreshPipeline()
    } finally {
      setBusy(false)
    }
  }

  const testSpeakers = async () => {
    if (busy) return
    setBusy(true)
    setTestingSpeaker(true)
    setAudioFeedback(null)
    try {
      const result = await jarvisClient.testSpeaker()
      if (!result.ok) {
        setAudioFeedback({ tone: 'error', message: result.error })
      }
    } finally {
      setTestingSpeaker(false)
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6 px-6 pb-6 pt-2">
      <PanelSection className="p-4">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="flex min-w-0 items-start gap-3">
            <StatusDot status={summary.tone} size="md" className="mt-1.5" />
            <div className="min-w-0">
              <h3 className="type-heading text-foreground">{summary.label}</h3>
              <p className="mt-1 type-body text-foreground-muted">{summary.detail}</p>
            </div>
          </div>
          <Button
            size="sm"
            variant={micReady ? 'ghost' : 'default'}
            color={micReady ? 'neutral' : 'brand'}
            disabled={busy}
            onClick={() => void (micReady ? testSpeakers() : enableMicrophone())}
          >
            {micReady ? (testingSpeaker ? 'Playing…' : 'Test speakers') : 'Enable microphone'}
          </Button>
        </div>

        <dl className="mt-4 grid gap-3 border-t border-outline/15 pt-4 sm:grid-cols-3">
          <div>
            <dt className="type-meta text-foreground-subtle">Listening</dt>
            <dd className="mt-1 type-body text-foreground">{selectedInput?.label || 'Default microphone'}</dd>
          </div>
          <div>
            <dt className="type-meta text-foreground-subtle">Understanding</dt>
            <dd className="mt-1 type-body text-foreground">{understanding}</dd>
          </div>
          <div>
            <dt className="type-meta text-foreground-subtle">Replies</dt>
            <dd className="mt-1 type-body text-foreground">{replies}</dd>
          </div>
        </dl>

        {devices.error && <p className="mt-3 type-body text-status-danger">{devices.error}</p>}
        {audioFeedback && (
          <p
            className={cn(
              'mt-3 type-body',
              audioFeedback.tone === 'success' ? 'text-status-success' : 'text-status-danger',
            )}
            role={audioFeedback.tone === 'error' ? 'alert' : 'status'}
          >
            {audioFeedback.message}
          </p>
        )}
      </PanelSection>

      <section className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h4 className="type-heading text-foreground">Devices</h4>
            <p className="mt-1 type-body text-foreground-muted">
              Choose where JARV1S listens and speaks on this device.
            </p>
          </div>
          <Button
            size="sm"
            variant="ghost"
            color="neutral"
            disabled={busy}
            icon={<ArrowsClockwiseIcon size={15} />}
            onClick={() => void run(async () => {
              await jarvisClient.refreshAudioDevices()
              await refreshPipeline()
            })}
          >
            Rescan
          </Button>
        </div>
        <DeviceSection
          title="Listen from"
          icon={<MicrophoneIcon size={16} />}
          value={devices.selectedInputId}
          devices={devices.inputs}
          disabled={busy}
          onChange={(id) => void run(() => jarvisClient.selectAudioInput(id))}
        />
        <DeviceSection
          title="Speak through"
          icon={<SpeakerHighIcon size={16} />}
          value={devices.selectedOutputId}
          devices={devices.outputs}
          disabled={busy || !devices.outputSelectionSupported}
          onChange={(id) => void run(() => jarvisClient.selectAudioOutput(id))}
          helper={
            !devices.outputSelectionSupported
              ? 'Speaker selection is managed by this device.'
              : undefined
          }
        />
      </section>

      <section className="space-y-3">
        <div>
          <h4 className="type-heading text-foreground">Voice behavior</h4>
          <p className="mt-1 type-body text-foreground-muted">
            Choose how JARV1S understands you and whether replies are spoken.
          </p>
        </div>
        <VoiceProviderSettings onConfigChange={() => void refreshPipeline()} />
      </section>

      <section className="space-y-3">
        <div>
          <h4 className="type-heading text-foreground">Your voice</h4>
          <p className="mt-1 type-body text-foreground-muted">
            Teach JARV1S to prefer your voice for wake and interrupt. You can re-record or remove it anytime.
          </p>
        </div>
        <VoiceEnrollmentCard />
        <WakePhraseCheckCard />
      </section>

      <div className="overflow-hidden rounded-panel bg-surface/10 shadow-[inset_0_1px_0_rgba(255,255,255,0.025)]">
        <button
          type="button"
          className="flex min-h-14 w-full items-center justify-between gap-3 px-4 py-4 text-left transition-colors duration-feedback hover:bg-surface/10 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand/70"
          aria-expanded={advancedOpen}
          onClick={() => setAdvancedOpen((open) => !open)}
        >
          <span>
            <span className="block type-heading text-foreground">Advanced voice settings</span>
            <span className="mt-0.5 block type-body text-foreground-muted">
              Echo cancellation and tool cue sounds.
            </span>
          </span>
          <CaretDownIcon
            size={18}
            className={cn('shrink-0 text-foreground-subtle transition-transform duration-feedback motion-reduce:transition-none', advancedOpen && 'rotate-180')}
            aria-hidden
          />
        </button>

        {advancedOpen && (
          <div className="space-y-4 border-t border-outline/15 p-4">
            <ProcessingProfileSection
              value={devices.processingProfile}
              appliedEchoCancellation={devices.appliedEchoCancellation}
              automaticCallCompatibility={devices.automaticCallCompatibility}
              activeCallApp={devices.activeCallApp}
              disabled={busy}
              onChange={(profile) => void run(() => jarvisClient.selectAudioProcessingProfile(profile))}
            />
            <PanelSection className="p-4">
              <Switch
                checked={toolCues}
                onChange={(checked) => {
                  void jarvisClient.setToolCuesEnabled(checked)
                }}
                label="Tool cues"
                description="Play a short sound when JARV1S starts tool work."
              />
            </PanelSection>
          </div>
        )}
      </div>
    </div>
  )
}

const PROCESSING_PROFILE_OPTIONS: Array<{
  value: AudioProcessingProfile
  label: string
  description: string
}> = [
  {
    value: 'standard',
    label: 'Standard — echo cancellation',
    description: 'Best with speakers; supports barge-in.',
  },
  {
    value: 'call_compatibility',
    label: 'Call compatibility — raw microphone',
    description: 'Use with Zoom/Teams and headphones. Speaker audio may be heard by JARV1S.',
  },
]

interface ProcessingProfileSectionProps {
  value: AudioProcessingProfile
  appliedEchoCancellation: boolean | null
  automaticCallCompatibility: boolean
  activeCallApp: string | null
  disabled: boolean
  onChange: (profile: AudioProcessingProfile) => void
}

const ProcessingProfileSection: React.FC<ProcessingProfileSectionProps> = ({
  value,
  appliedEchoCancellation,
  automaticCallCompatibility,
  activeCallApp,
  disabled,
  onChange,
}) => {
  const fieldId = 'audio-processing-profile'
  const expectedEchoCancellation = automaticCallCompatibility ? false : value === 'standard'
  const processingWarning =
    appliedEchoCancellation !== null && appliedEchoCancellation !== expectedEchoCancellation
      ? expectedEchoCancellation
        ? 'Browser did not enable echo cancellation. Speaker barge-in may not work reliably.'
        : 'Browser still applied echo cancellation. Zoom/Teams mic quality may be affected.'
      : null

  return (
    <FieldControl label="Microphone processing" htmlFor={fieldId} className="w-full">
      <Select
        id={fieldId}
        aria-label="Microphone processing"
        value={value}
        disabled={disabled}
        onChange={(next) => onChange(next as AudioProcessingProfile)}
        className="w-full"
        options={PROCESSING_PROFILE_OPTIONS.map((option) => ({
          value: option.value,
          label: option.label,
          description: option.description,
        }))}
      />
      <p className="type-body text-foreground-muted">
        {PROCESSING_PROFILE_OPTIONS.find((option) => option.value === value)?.description}
      </p>
      {automaticCallCompatibility && (
        <p className="mt-1 text-sm text-status-success" role="status">
          Call compatibility is active for {activeCallApp ?? 'your call'}. Your saved profile returns when the call ends.
        </p>
      )}
      {processingWarning && (
        <p className="mt-1 text-sm text-status-warning" role="status">
          {processingWarning}
        </p>
      )}
    </FieldControl>
  )
}

interface DeviceSectionProps {
  title: string
  icon: React.ReactNode
  value: string
  devices: Array<{ deviceId: string; label: string }>
  disabled: boolean
  helper?: string
  onChange: (id: string) => void
}

const DeviceSection: React.FC<DeviceSectionProps> = ({
  title,
  icon,
  value,
  devices,
  disabled,
  helper,
  onChange,
}) => {
  const fieldId = `audio-device-${title.toLowerCase().replace(/\s+/g, '-')}`

  return (
    <FieldControl label={title} hint={helper} htmlFor={fieldId}>
      <div className="flex items-center gap-3">
        <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-control bg-surface/20 text-brand" aria-hidden>
          {icon}
        </span>
        <Select
          id={fieldId}
          aria-label={title}
          value={value}
          disabled={disabled}
          onChange={onChange}
          className="min-w-0 flex-1"
          options={devices.map((device) => ({
            value: device.deviceId,
            label: device.label,
          }))}
        />
      </div>
    </FieldControl>
  )
}
