/**
 * REST client for host-level voice runtime settings and owner speaker profile.
 */

import { authorizedFetch } from './http'

const API_BASE = '/api/v1/voice'

export class VoiceApiError extends Error {
  reason: string | null
  clipIndex: number | null

  constructor(message: string, reason: string | null = null, clipIndex: number | null = null) {
    super(message)
    this.name = 'VoiceApiError'
    this.reason = reason
    this.clipIndex = clipIndex
  }
}

function bytesToBase64(bytes: Uint8Array): string {
  const chunkSize = 0x8000
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
  }
  return btoa(binary)
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const isFormData = body instanceof FormData
  const res = await authorizedFetch(`${API_BASE}${path}`, {
    method,
    headers: isFormData ? undefined : { 'Content-Type': 'application/json' },
    cache: 'no-store',
    body: body === undefined ? undefined : isFormData ? body : JSON.stringify(body),
  })

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    let reason: string | null = null
    let clipIndex: number | null = null
    try {
      const json = await res.json()
      if (json?.detail && typeof json.detail === 'object') {
        reason = typeof json.detail.reason === 'string' ? json.detail.reason : null
        clipIndex = typeof json.detail.clip_index === 'number' ? json.detail.clip_index : null
        detail = typeof json.detail.message === 'string' ? json.detail.message : detail
      } else {
        detail = json.detail ?? detail
      }
    } catch {
      // ignore
    }
    throw new VoiceApiError(
      typeof detail === 'string' ? detail : `HTTP ${res.status}`,
      reason,
      clipIndex,
    )
  }

  if (res.status === 204) {
    return undefined as T
  }
  return res.json() as Promise<T>
}

export type STTProvider = 'apple_speech' | 'cartesia'
export type TTSProvider = 'off' | 'cartesia' | 'local'
export type SpeakerProfileStatusValue = 'not_enrolled' | 'enrolled'
export type WakeCheckStatus = 'recognized' | 'not_detected' | 'speaker_mismatch'
export type VoiceInputState =
  | 'ready'
  | 'needs_permission'
  | 'needs_assets'
  | 'unavailable'
  | 'missing_key'
  | 'unsupported'
export type VoiceOutputState =
  | 'ready'
  | 'unavailable'
  | 'missing_key'
  | 'needs_voice'
  | 'unsupported'

export interface VoiceRuntimeConfig {
  stt_provider: STTProvider
  tts_provider: TTSProvider
  cartesia_voice_id: string | null
  local_voice_id: string
  source: 'persisted' | 'default'
}

export interface UpdateVoiceRuntimeConfigRequest {
  stt_provider?: STTProvider
  tts_provider?: TTSProvider
  cartesia_voice_id?: string | null
  local_voice_id?: string | null
}

export interface VoiceInputStatus {
  provider: STTProvider
  ready: boolean
  state: VoiceInputState
  detail: string | null
}

export interface VoiceOutputStatus {
  provider: TTSProvider
  ready: boolean
  state: VoiceOutputState
  detail: string | null
}

export interface LocalVoicePreview {
  audio: string
  sample_rate: number
}

export interface SpeakerProfileStatus {
  status: SpeakerProfileStatusValue
  updated_at: string | null
}

export interface UpsertSpeakerProfileRequest {
  clips: Uint8Array[]
}

export interface WakeCheckResult {
  status: WakeCheckStatus
}

export const voiceApi = {
  getConfig: () => request<VoiceRuntimeConfig>('GET', '/config'),
  updateConfig: (body: UpdateVoiceRuntimeConfigRequest) =>
    request<VoiceRuntimeConfig>('PATCH', '/config', body),
  getInputStatus: (provider?: STTProvider, options?: { force?: boolean }) => {
    const params = new URLSearchParams()
    if (provider) params.set('provider', provider)
    if (options?.force) params.set('force', 'true')
    const query = params.toString()
    return request<VoiceInputStatus>('GET', `/input/status${query ? `?${query}` : ''}`)
  },
  prepareInput: () => request<VoiceInputStatus>('POST', '/input/prepare'),
  getOutputStatus: (provider?: TTSProvider) =>
    request<VoiceOutputStatus>('GET', `/output/status${provider ? `?provider=${provider}` : ''}`),
  previewLocalVoice: () => request<LocalVoicePreview>('POST', '/output/preview'),
  cloneVoice: (clip: File, name = 'JARV1S voice', language = 'en') => {
    const body = new FormData()
    body.append('clip', clip)
    body.append('name', name)
    body.append('language', language)
    return request<VoiceRuntimeConfig>('POST', '/clone', body)
  },
  getSpeakerProfile: () => request<SpeakerProfileStatus>('GET', '/speaker-profile'),
  upsertSpeakerProfile: (body: UpsertSpeakerProfileRequest) =>
    request<SpeakerProfileStatus>('PUT', '/speaker-profile', {
      clips: body.clips.map(bytesToBase64),
    }),
  deleteSpeakerProfile: () => request<SpeakerProfileStatus>('DELETE', '/speaker-profile'),
  checkWakePhrase: (clip: Uint8Array) =>
    request<WakeCheckResult>('POST', '/wake-check', { clip: bytesToBase64(clip) }),
}
