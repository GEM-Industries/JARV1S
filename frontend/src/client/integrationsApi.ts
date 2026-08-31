/**
 * Lightweight REST client for the integrations management API.
 * Used only by the IntegrationsPanel — kept separate from JarvisClient
 * (which is WebSocket-only) to keep concerns clean.
 */

import { authorizedFetch } from './http'

import type { IntegrationSummary } from '../types'

const API_BASE = '/api/v1/integrations'

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await authorizedFetch(`${API_BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      detail = json.detail ?? detail
    } catch {
      // ignore parse errors
    }
    throw new Error(detail)
  }

  return res.json() as Promise<T>
}

export interface IntegrationList {
  items: IntegrationSummary[]
}

export interface ConnectLinkResponse {
  name: string
  connect_url: string
}

export interface ActionResult {
  success: boolean
  message: string
}

export interface CatalogItem {
  slug: string
  display_name: string
  description: string
  auth_type: string
  connected: boolean
  managed_auth: boolean
}

export interface CatalogList {
  items: CatalogItem[]
}

export const integrationsApi = {
  list(): Promise<IntegrationList> {
    return request<IntegrationList>('GET', '')
  },

  get(name: string): Promise<IntegrationSummary> {
    return request<IntegrationSummary>('GET', `/${encodeURIComponent(name)}`)
  },

  connectLink(name: string): Promise<ConnectLinkResponse> {
    return request<ConnectLinkResponse>('POST', `/${name}/connect-link`)
  },

  disconnect(name: string): Promise<ActionResult> {
    return request<ActionResult>('DELETE', `/${name}`)
  },

  reconcile(name: string): Promise<ActionResult> {
    return request<ActionResult>('POST', `/${name}/reconcile`)
  },

  refresh(): Promise<ActionResult> {
    return request<ActionResult>('POST', '/refresh')
  },

  searchCatalog(query: string): Promise<CatalogList> {
    const qs = query ? `?q=${encodeURIComponent(query)}` : ''
    return request<CatalogList>('GET', `/catalog${qs}`)
  },

  toggle(name: string, enabled: boolean): Promise<ActionResult> {
    return request<ActionResult>('PATCH', `/${name}/toggle`, { enabled })
  },

  authorizeMacosCalendar(): Promise<ActionResult> {
    return request<ActionResult>('POST', '/calendar/macos')
  },
}
