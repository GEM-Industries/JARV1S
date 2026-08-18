/**
 * REST client for Jarvis Host first-run setup.
 * Separate from JarvisClient (WebSocket) — setup runs before pairing/connect.
 */

const API_BASE = '/api/v1/setup'

export class SetupApiError extends Error {
  constructor(
    message: string,
    readonly validation?: ValidationResult,
  ) {
    super(message)
    this.name = 'SetupApiError'
  }
}

function isValidationResult(value: unknown): value is ValidationResult {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Record<string, unknown>
  return typeof candidate.ok === 'boolean' && typeof candidate.message === 'string'
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    let validation: ValidationResult | undefined
    try {
      const json: unknown = await res.json()
      if (json && typeof json === 'object' && 'detail' in json) {
        const responseDetail = (json as Record<string, unknown>).detail
        if (typeof responseDetail === 'string') detail = responseDetail
        if (isValidationResult(responseDetail)) {
          validation = responseDetail
          detail = responseDetail.message
        }
      }
    } catch {
      // ignore
    }
    throw new SetupApiError(detail, validation)
  }

  return res.json() as Promise<T>
}

export type ReadinessPhase = 'needs_setup' | 'initializing' | 'ready' | 'degraded'

export type ValidationFailureCode =
  | 'missing_key'
  | 'placeholder_key'
  | 'bad_key'
  | 'permission_denied'
  | 'quota_or_billing'
  | 'rate_limited'
  | 'bad_endpoint'
  | 'model_unavailable'
  | 'network_unreachable'
  | 'timeout'
  | 'provider_unavailable'
  | 'unknown'

export interface ServiceStatus {
  name: string
  status: 'up' | 'down' | 'not_configured' | 'optional'
  detail?: string | null
}

export interface LlmSetupStatus {
  provider: string
  configured: boolean
  source?: string | null
  masked_suffix?: string | null
  model?: string | null
}

export type LaneType =
  | 'keyless'
  | 'api_key_optional'
  | 'oauth_consent'
  | 'brokered_connect'
  | 'manual_handoff'
  | 'local_service'

export type LaneStatus =
  | 'ready'
  | 'configured'
  | 'needs_action'
  | 'degraded'
  | 'unavailable'
  | 'optional'

export interface CapabilityLaneStatus {
  id: string
  label: string
  lane_type: LaneType
  status: LaneStatus
  detail?: string | null
}

export interface SetupState {
  role: 'host_local'
  phase: ReadinessPhase
  core_ready: boolean
  chat_enabled: boolean
  voice_enabled: boolean
  services: ServiceStatus[]
  llm: LlmSetupStatus
  capability_lanes: CapabilityLaneStatus[]
  blocking_reason?: string | null
  next_action?: string | null
}

export interface LlmProviderOption {
  id: string
  label: string
  signup_url: string
  default_model: string
  recommended_model: string
  stability: 'stable' | 'preview'
  credential_names: string[]
  key_stored: boolean
  masked_suffix?: string | null
}

export interface LocalLlmRuntime {
  runtime: string
  label: string
  base_url: string
  reachable: boolean
  models: string[]
  detail?: string | null
}

export interface ConfigureLlmRequest {
  provider: string
  api_key?: string
  model?: string
  base_url?: string
}

export interface ValidationResult {
  ok: boolean
  code?: ValidationFailureCode | null
  message: string
  next_action?: string | null
  recommended_model?: string | null
}

export interface RuntimeInitResponse {
  phase: ReadinessPhase
  core_ready: boolean
  message: string
}

export type ManagedLlmStatusKind =
  | 'unsupported'
  | 'runtime_down'
  | 'absent'
  | 'downloading'
  | 'ready'
  | 'failed'

export interface ManagedLlmStatus {
  status: ManagedLlmStatusKind
  runtime_ready: boolean
  model_id: string
  model_label: string
  model_installed: boolean
  model_size_bytes: number
  approx_download_bytes: number
  min_memory_bytes: number
  min_disk_bytes: number
  supported: boolean
  model_license_url: string
  completed_bytes: number
  total_bytes: number
  detail?: string | null
  active: boolean
}

export interface ActivateLlmResponse {
  phase: ReadinessPhase
  core_ready: boolean
  message: string
  state: SetupState
}

export const setupApi = {
  getState: () => request<SetupState>('GET', '/state'),
  listProviders: () => request<LlmProviderOption[]>('GET', '/providers'),
  discoverLocalLlms: () => request<LocalLlmRuntime[]>('GET', '/llm/local/discover'),
  getManagedLocalStatus: () => request<ManagedLlmStatus>('GET', '/llm/local/managed/status'),
  installManagedLocal: () => request<ManagedLlmStatus>('POST', '/llm/local/managed/install'),
  cancelManagedLocal: () => request<ManagedLlmStatus>('POST', '/llm/local/managed/cancel'),
  removeManagedLocal: () => request<ManagedLlmStatus>('DELETE', '/llm/local/managed/model'),
  activateManagedLocal: () => request<ActivateLlmResponse>('POST', '/llm/local/managed/activate'),
  activateLlm: (body: ConfigureLlmRequest) => request<ActivateLlmResponse>('POST', '/llm/activate', body),
  testLlm: (body: ConfigureLlmRequest) => request<ValidationResult>('POST', '/llm/test', body),
  initializeRuntime: () => request<RuntimeInitResponse>('POST', '/runtime/initialize'),
}
