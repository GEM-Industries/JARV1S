/**
 * External ingress + durable inbound event operations.
 */

import { authorizedFetch } from './http'

const API_BASE = '/api/v1/ingress'

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

  if (res.status === 204) {
    return undefined as T
  }
  return res.json() as Promise<T>
}

export interface ExternalIngressState {
  enabled: boolean
  provider: 'tailscale_funnel' | 'custom' | 'none'
  base_url?: string | null
  composio_subscription_ok: boolean
  secret_present: boolean
  push_channels_ok: boolean
  last_error?: string | null
  last_received_at?: string | null
  last_reconciled_at?: string | null
  inbox_pending: number
  inbox_dead_letter: number
  detail?: string | null
}

export interface InboundEventStats {
  pending: number
  processing: number
  retry: number
  processed: number
  dead_letter: number
  oldest_pending_age_s?: number | null
  last_received_at?: string | null
  last_processed_at?: string | null
}

export interface InboundEventSummary {
  id: string
  kind: 'composio' | 'push' | 'external'
  source: string
  status: 'pending' | 'processing' | 'retry' | 'processed' | 'dead_letter'
  attempts: number
  last_error?: string | null
  received_at: string
  processed_at?: string | null
  next_attempt_at?: string | null
}

export const ingressApi = {
  get: () => request<ExternalIngressState>('GET', '/external'),
  stats: () => request<InboundEventStats>('GET', '/events/stats'),
  deadLetters: (limit = 20) =>
    request<InboundEventSummary[]>('GET', `/events/dead-letters?limit=${limit}`),
  recent: (limit = 20) =>
    request<InboundEventSummary[]>('GET', `/events/recent?limit=${limit}`),
  retry: (eventId: string) =>
    request<InboundEventSummary>('POST', `/events/${encodeURIComponent(eventId)}/retry`),
}
