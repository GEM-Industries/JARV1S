/**
 * REST client for product credential management.
 */

import { authorizedFetch } from './http'

const API_BASE = '/api/v1/credentials'

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await authorizedFetch(`${API_BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    cache: 'no-store',
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      detail = json.detail ?? detail
    } catch {
      // ignore
    }
    throw new Error(detail)
  }

  return res.json() as Promise<T>
}

export type CredentialCardStatus = 'missing' | 'stored' | 'env_deprecated'

export interface CredentialCard {
  id: string
  label: string
  description: string
  secret_name: string
  status: CredentialCardStatus
  source?: string | null
  masked_suffix?: string | null
  next_action?: string | null
  detail?: string | null
}

export interface ExternalTriggersStatus {
  enabled: boolean
  base_url: string
  provider: string
  last_received_at?: string | null
  inbox_pending: number
  inbox_dead_letter: number
  last_error?: string | null
  detail: string
}

export interface CredentialsListResponse {
  items: CredentialCard[]
  external_triggers: ExternalTriggersStatus
}

export interface CredentialActionResult {
  ok: boolean
  message: string
  card: CredentialCard
}

export interface CredentialValidationResult {
  ok: boolean
  message: string
}

export const credentialsApi = {
  list: () => request<CredentialsListResponse>('GET', '/'),
  save: (id: string, value: string) =>
    request<CredentialActionResult>('PUT', `/${id}`, { value }),
  remove: (id: string) => request<CredentialActionResult>('DELETE', `/${id}`),
  validate: (id: string, value: string) =>
    request<CredentialValidationResult>('POST', `/${id}/validate`, { value }),
}
