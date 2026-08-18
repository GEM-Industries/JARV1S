import { describe, expect, it, vi } from 'vitest'
import type {
  VoiceInputStatus,
  VoiceOutputStatus,
  VoiceRuntimeConfig,
} from '../../client/voiceApi'
import {
  DOWNLOAD_SPEECH_LABEL,
  START_VOICE_LABEL,
  isUntouchedVoiceOutputDefault,
  prepareVoiceEntry,
  resolveVoiceControlPresentation,
  shouldEnableLocalTtsOnStart,
  summarizeVoicePipeline,
  type VoiceEntryDeps,
} from './voiceEntry'

function config(partial: Partial<VoiceRuntimeConfig> = {}): VoiceRuntimeConfig {
  return {
    stt_provider: 'apple_speech',
    tts_provider: 'off',
    cartesia_voice_id: null,
    local_voice_id: 'af_heart',
    source: 'default',
    ...partial,
  }
}

function input(partial: Partial<VoiceInputStatus> = {}): VoiceInputStatus {
  return {
    provider: 'apple_speech',
    ready: true,
    state: 'ready',
    detail: null,
    ...partial,
  }
}

function output(partial: Partial<VoiceOutputStatus> = {}): VoiceOutputStatus {
  return {
    provider: 'local',
    ready: true,
    state: 'ready',
    detail: null,
    ...partial,
  }
}

describe('voice entry presentation', () => {
  it('never shows Mute when audio is not ready', () => {
    const presentation = resolveVoiceControlPresentation({
      connected: true,
      isAudioContextReady: false,
      isMuted: false,
      preparing: false,
      issue: null,
    })
    expect(presentation.action).toBe('start')
    expect(presentation.text).toBe(START_VOICE_LABEL)
    expect(presentation.detail).toBeNull()
  })

  it('shows preparing and blocked labels without becoming Mute', () => {
    expect(resolveVoiceControlPresentation({
      connected: true,
      isAudioContextReady: false,
      isMuted: true,
      preparing: true,
      issue: null,
    })).toMatchObject({
      action: 'preparing',
      text: 'Preparing voice…',
      detail: null,
    })

    expect(resolveVoiceControlPresentation({
      connected: true,
      isAudioContextReady: false,
      isMuted: false,
      preparing: false,
      issue: { kind: 'blocked', detail: 'Helper down' },
    })).toMatchObject({
      action: 'setup',
      text: 'Set up voice',
      detail: 'Helper down',
    })
  })

  it('offers a download CTA when on-device speech assets are missing', () => {
    expect(resolveVoiceControlPresentation({
      connected: true,
      isAudioContextReady: false,
      isMuted: false,
      preparing: false,
      issue: { kind: 'download', detail: 'On-device speech needs a one-time download.' },
    })).toMatchObject({
      action: 'download',
      text: DOWNLOAD_SPEECH_LABEL,
    })
  })

  it('uses Unmute mic / Mute mic only after microphone is ready', () => {
    expect(resolveVoiceControlPresentation({
      connected: true,
      isAudioContextReady: true,
      isMuted: true,
      preparing: false,
      issue: null,
    })).toMatchObject({
      action: 'resume',
      text: 'Unmute mic',
      tone: 'warning',
    })
    expect(resolveVoiceControlPresentation({
      connected: true,
      isAudioContextReady: true,
      isMuted: false,
      preparing: false,
      issue: null,
    })).toMatchObject({
      action: 'mute',
      text: 'Mute mic',
      tone: 'brand',
    })
  })

  it('ignores stale issues once the microphone is live', () => {
    expect(resolveVoiceControlPresentation({
      connected: true,
      isAudioContextReady: true,
      isMuted: false,
      preparing: false,
      issue: { kind: 'error', detail: 'stale' },
    })).toMatchObject({
      action: 'mute',
      text: 'Mute mic',
    })
  })
})

describe('voice entry TTS defaults', () => {
  it('treats only untouched defaults as eligible for local TTS', () => {
    expect(isUntouchedVoiceOutputDefault(config())).toBe(true)
    expect(isUntouchedVoiceOutputDefault(config({ source: 'persisted', tts_provider: 'off' }))).toBe(false)
    expect(isUntouchedVoiceOutputDefault(config({ tts_provider: 'local' }))).toBe(false)
  })

  it('enables local TTS only when the helper is healthy', () => {
    expect(shouldEnableLocalTtsOnStart(config(), output({ ready: true }))).toBe(true)
    expect(shouldEnableLocalTtsOnStart(config(), output({ ready: false }))).toBe(false)
    expect(shouldEnableLocalTtsOnStart(config({ source: 'persisted', tts_provider: 'off' }), output())).toBe(false)
  })
})

describe('prepareVoiceEntry', () => {
  it('does not overwrite an explicit text-only choice', async () => {
    const updateConfig = vi.fn()
    const deps: VoiceEntryDeps = {
      getConfig: async () => config({ source: 'persisted', tts_provider: 'off' }),
      getInputStatus: async () => input(),
      prepareInput: async () => input(),
      getOutputStatus: async () => output(),
      updateConfig,
    }

    const result = await prepareVoiceEntry(deps)
    expect(result.ok).toBe(true)
    expect(updateConfig).not.toHaveBeenCalled()
    expect(result.pendingLocalTts).toBeFalsy()
  })

  it('flags healthy local TTS for untouched defaults without persisting yet', async () => {
    const updateConfig = vi.fn(async () => config({ source: 'persisted', tts_provider: 'local' }))
    const deps: VoiceEntryDeps = {
      getConfig: async () => config(),
      getInputStatus: async () => input(),
      prepareInput: async () => input(),
      getOutputStatus: async () => output({ ready: true }),
      updateConfig,
    }

    const result = await prepareVoiceEntry(deps)
    expect(result.ok).toBe(true)
    expect(updateConfig).not.toHaveBeenCalled()
    expect(result.pendingLocalTts).toBe(true)
  })

  it('keeps a transient STT outage retryable instead of activating voice', async () => {
    const result = await prepareVoiceEntry({
      getConfig: async () => config(),
      getInputStatus: async () => input({
        ready: false,
        state: 'unavailable',
        detail: 'Speech helper is offline',
      }),
      prepareInput: async () => input({ ready: false, state: 'unavailable' }),
      getOutputStatus: async () => output(),
      updateConfig: async () => config(),
    })

    expect(result.ok).toBe(false)
    expect(result.issue?.kind).toBe('error')
    expect(result.detail).toContain('offline')
  })

  it('requests Apple Speech permission when it has not been granted', async () => {
    const prepareInput = vi.fn(async () => input({ ready: true }))
    const result = await prepareVoiceEntry({
      getConfig: async () => config(),
      getInputStatus: async () => input({
        ready: false,
        state: 'needs_permission',
        detail: 'Permission required',
      }),
      prepareInput,
      getOutputStatus: async () => output({ ready: false }),
      updateConfig: async () => config(),
    })

    expect(prepareInput).toHaveBeenCalledOnce()
    expect(result.ok).toBe(true)
  })

  it('sends denied Apple Speech permission to setup', async () => {
    const result = await prepareVoiceEntry({
      getConfig: async () => config(),
      getInputStatus: async () => input({
        ready: false,
        state: 'needs_permission',
        detail: 'Permission required',
      }),
      prepareInput: async () => input({
        ready: false,
        state: 'needs_permission',
        detail: 'Permission denied',
      }),
      getOutputStatus: async () => output({ ready: false }),
      updateConfig: async () => config(),
    })

    expect(result.issue).toEqual({ kind: 'blocked', detail: 'Permission denied' })
  })

  it('keeps missing speech assets on a download path instead of activating voice', async () => {
    const result = await prepareVoiceEntry({
      getConfig: async () => config(),
      getInputStatus: async () => input({
        ready: false,
        state: 'needs_assets',
        detail: 'On-device speech needs a one-time download.',
      }),
      prepareInput: async () => input({
        ready: false,
        state: 'needs_assets',
        detail: 'On-device speech needs a one-time download.',
      }),
      getOutputStatus: async () => output(),
      updateConfig: async () => config(),
    })

    expect(result.ok).toBe(false)
    expect(result.issue?.kind).toBe('download')
  })

  it('prepares apple speech assets when needed', async () => {
    const prepareInput = vi.fn(async () => input({ ready: true }))
    const result = await prepareVoiceEntry({
      getConfig: async () => config(),
      getInputStatus: async () => input({ ready: false, state: 'needs_assets', detail: 'Downloading' }),
      prepareInput,
      getOutputStatus: async () => output({ ready: false }),
      updateConfig: async () => config(),
    })

    expect(prepareInput).toHaveBeenCalled()
    expect(result.ok).toBe(true)
  })
})

describe('summarizeVoicePipeline', () => {
  it('prioritizes STT unreadiness over microphone permission', () => {
    const summary = summarizeVoicePipeline({
      micReady: false,
      muted: false,
      config: config(),
      input: input({ ready: false, detail: 'Speech helper offline' }),
      output: null,
    })
    expect(summary.label).toBe('Needs voice input setup')
  })

  it('reports microphone need when input is ready', () => {
    const summary = summarizeVoicePipeline({
      micReady: false,
      muted: false,
      config: config(),
      input: input({ ready: true }),
      output: null,
    })
    expect(summary.label).toBe('Needs microphone access')
  })
})
