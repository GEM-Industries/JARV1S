/**
 * Pure voice-start rules and settings pipeline summary.
 * Composes voiceApi + mic permission; React wiring lives in useVoiceEntry.
 */

import type {
  STTProvider,
  TTSProvider,
  UpdateVoiceRuntimeConfigRequest,
  VoiceInputStatus,
  VoiceOutputStatus,
  VoiceRuntimeConfig,
} from '../../client/voiceApi'

/** Footer control label — keep EmptyChatState in sync via this export. */
export const START_VOICE_LABEL = 'Start voice'
export const DOWNLOAD_SPEECH_LABEL = 'Download on-device speech'

export type VoiceControlAction = 'start' | 'preparing' | 'mute' | 'resume' | 'setup' | 'retry' | 'download'
export type VoiceControlTone = 'brand' | 'warning' | 'critical' | 'neutral'

export interface VoiceEntryIssue {
  kind: 'blocked' | 'error' | 'download'
  detail: string
}

export interface VoiceEntryResult {
  ok: boolean
  issue?: VoiceEntryIssue
  detail: string
  config?: VoiceRuntimeConfig
  inputStatus?: VoiceInputStatus
  /** Healthy local TTS is available and defaults are untouched — apply after mic grant. */
  pendingLocalTts?: boolean
}

export interface VoiceEntryDeps {
  getConfig: () => Promise<VoiceRuntimeConfig>
  getInputStatus: (
    provider?: STTProvider,
    options?: { force?: boolean },
  ) => Promise<VoiceInputStatus>
  prepareInput: () => Promise<VoiceInputStatus>
  getOutputStatus: (provider?: TTSProvider) => Promise<VoiceOutputStatus>
  updateConfig: (body: UpdateVoiceRuntimeConfigRequest) => Promise<VoiceRuntimeConfig>
}

export interface VoiceControlPresentation {
  action: VoiceControlAction
  text: string
  tone: VoiceControlTone
  detail: string | null
  disabled: boolean
}

/** Untouched product default: never persisted, spoken replies still off. */
export function isUntouchedVoiceOutputDefault(config: VoiceRuntimeConfig): boolean {
  return config.source === 'default' && config.tts_provider === 'off'
}

export function shouldEnableLocalTtsOnStart(
  config: VoiceRuntimeConfig,
  localOutput: VoiceOutputStatus | null,
): boolean {
  return isUntouchedVoiceOutputDefault(config) && localOutput?.ready === true
}

/**
 * Prepare the selected STT path and, when defaults are untouched, enable
 * healthy local TTS. Never overwrite an explicit spoken-replies choice.
 */
export async function prepareVoiceEntry(deps: VoiceEntryDeps): Promise<VoiceEntryResult> {
  let config: VoiceRuntimeConfig
  try {
    config = await deps.getConfig()
  } catch (err) {
    return {
      ok: false,
      issue: {
        kind: 'error',
        detail: err instanceof Error ? err.message : 'Could not load voice settings.',
      },
      detail: err instanceof Error ? err.message : 'Could not load voice settings.',
    }
  }

  let inputStatus: VoiceInputStatus
  try {
    // Force a fresh probe on user action so a stale startup cache cannot
    // block Start voice after the helper becomes ready.
    inputStatus = await deps.getInputStatus(config.stt_provider, { force: true })
  } catch (err) {
    return {
      ok: false,
      issue: {
        kind: 'error',
        detail: err instanceof Error ? err.message : 'Could not check voice input.',
      },
      detail: err instanceof Error ? err.message : 'Could not check voice input.',
    }
  }

  const appleSpeechNeedsPreparation =
    config.stt_provider === 'apple_speech'
    && (inputStatus.state === 'needs_permission' || inputStatus.state === 'needs_assets')
  if (!inputStatus.ready && appleSpeechNeedsPreparation) {
    try {
      inputStatus = await deps.prepareInput()
    } catch (err) {
      const detail = err instanceof Error ? err.message : 'Could not prepare on-device speech.'
      return {
        ok: false,
        issue: { kind: 'error', detail },
        detail,
        config,
        inputStatus,
      }
    }
  }

  if (!inputStatus.ready) {
    const detail = inputStatus.detail
      ?? (inputStatus.state === 'missing_key'
        ? 'Cloud transcription needs a Cartesia key in Voice & Audio settings.'
        : inputStatus.state === 'needs_permission'
          ? 'On-device speech needs system permission first.'
          : inputStatus.state === 'needs_assets'
            ? 'On-device speech needs a one-time download.'
            : 'Voice input is not ready. You can keep typing, or open Voice & Audio settings.')
    const kind: VoiceEntryIssue['kind'] =
      inputStatus.state === 'needs_assets'
        ? 'download'
        : inputStatus.state === 'unsupported'
          || inputStatus.state === 'missing_key'
          || inputStatus.state === 'needs_permission'
            ? 'blocked'
            : 'error'
    return {
      ok: false,
      issue: { kind, detail },
      detail,
      config,
      inputStatus,
    }
  }

  let pendingLocalTts = false
  if (isUntouchedVoiceOutputDefault(config)) {
    let localOutput: VoiceOutputStatus | null = null
    try {
      localOutput = await deps.getOutputStatus('local')
    } catch {
      localOutput = null
    }
    pendingLocalTts = shouldEnableLocalTtsOnStart(config, localOutput)
  }

  return {
    ok: true,
    detail: pendingLocalTts
      ? 'Voice is ready. Spoken replies will use On this Mac after the microphone is allowed.'
      : 'Voice is ready.',
    config,
    inputStatus,
    pendingLocalTts,
  }
}

/**
 * ControlBar mic button. Idle orientation lives in EmptyChatState — only
 * actionable setup, download, and error states show detail under the composer.
 */
export function resolveVoiceControlPresentation(args: {
  connected: boolean
  isAudioContextReady: boolean
  isMuted: boolean
  preparing: boolean
  issue: VoiceEntryIssue | null
}): VoiceControlPresentation {
  const { connected, isAudioContextReady, isMuted, preparing, issue } = args

  if (!connected) {
    return {
      action: 'start',
      text: START_VOICE_LABEL,
      tone: 'brand',
      detail: null,
      disabled: true,
    }
  }

  if (preparing) {
    return {
      action: 'preparing',
      text: 'Preparing voice…',
      tone: 'brand',
      detail: null,
      disabled: true,
    }
  }

  if (issue && !isAudioContextReady) {
    if (issue.kind === 'download') {
      return {
        action: 'download',
        text: DOWNLOAD_SPEECH_LABEL,
        tone: 'neutral',
        detail: issue.detail,
        disabled: false,
      }
    }
    return {
      action: issue.kind === 'blocked' ? 'setup' : 'retry',
      text: issue.kind === 'blocked' ? 'Set up voice' : 'Retry voice',
      tone: 'neutral',
      detail: issue.detail,
      disabled: false,
    }
  }

  if (!isAudioContextReady) {
    return {
      action: 'start',
      text: START_VOICE_LABEL,
      tone: 'brand',
      detail: null,
      disabled: false,
    }
  }

  if (isMuted) {
    return {
      action: 'resume',
      text: 'Unmute mic',
      tone: 'warning',
      detail: null,
      disabled: false,
    }
  }

  return {
    action: 'mute',
    text: 'Mute mic',
    tone: 'brand',
    detail: null,
    disabled: false,
  }
}

export type VoicePipelineSummaryTone = 'success' | 'warning' | 'off' | 'error'

export function summarizeVoicePipeline(args: {
  micReady: boolean
  muted: boolean
  config: VoiceRuntimeConfig | null
  input: VoiceInputStatus | null
  output: VoiceOutputStatus | null
}): { label: string; detail: string; tone: VoicePipelineSummaryTone } {
  const { micReady, muted, config, input, output } = args

  if (input && !input.ready) {
    if (input.state === 'needs_assets') {
      return {
        label: 'Needs on-device speech download',
        detail: input.detail ?? 'Download on-device speech once, then start voice from chat.',
        tone: 'warning',
      }
    }
    return {
      label: 'Needs voice input setup',
      detail: input.detail ?? 'Fix understanding below, then start voice from chat.',
      tone: 'warning',
    }
  }

  if (!micReady) {
    return {
      label: 'Needs microphone access',
      detail: 'Start voice from chat when you are ready. The microphone stays off until then.',
      tone: 'off',
    }
  }

  if (muted) {
    return {
      label: 'Microphone muted',
      detail: 'Unmute mic from the chat footer to listen again.',
      tone: 'warning',
    }
  }

  const tts = config?.tts_provider ?? 'off'
  if (tts === 'off') {
    return {
      label: 'Ready to talk',
      detail: 'JARV1S can hear you. Spoken replies are off — answers show as text.',
      tone: 'success',
    }
  }

  if ((tts === 'local' || tts === 'cartesia') && output && !output.ready) {
    return {
      label: 'Ready to listen',
      detail: output.detail ?? 'Spoken replies need a quick fix. Text replies still work.',
      tone: 'warning',
    }
  }

  return {
    label: 'Ready to talk',
    detail: 'Microphone, understanding, and spoken replies are ready.',
    tone: 'success',
  }
}
