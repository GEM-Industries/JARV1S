/**
 * REST client for bespoke Google/Microsoft OAuth.
 */

import { authorizedFetch } from './http'

const API_BASE = '/api/v1/auth/oauth'

export class OAuthApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message)
    this.name = 'OAuthApiError'
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
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
    throw new OAuthApiError(detail, res.status)
  }

  return res.json() as Promise<T>
}

export interface ProviderStatus {
  provider: string
  connectable: boolean
  connected: boolean
  account_email: string | null
  config_mode?: 'product' | 'self_managed' | null
}

export interface AuthorizeResult {
  authorize_url: string
}

export const oauthApi = {
  getProviders(): Promise<ProviderStatus[]> {
    return request<ProviderStatus[]>('GET', '/providers')
  },

  configure(provider: string, clientId: string, clientSecret?: string): Promise<{ success: boolean }> {
    return request('POST', `/providers/${provider}/configure`, {
      client_id: clientId,
      client_secret: clientSecret ?? null,
    })
  },

  authorize(
    provider: string,
    origin: string,
    options?: { plugin?: string; scopes?: string[] },
  ): Promise<AuthorizeResult> {
    return request<AuthorizeResult>('POST', `/providers/${provider}/authorize`, {
      origin,
      ...(options?.plugin ? { plugin: options.plugin } : {}),
      ...(options?.scopes?.length ? { scopes: options.scopes } : {}),
    })
  },

  deleteProvider(provider: string): Promise<{ status: string }> {
    return request('DELETE', `/providers/${provider}`)
  },

  getProviderStatus(provider: string): Promise<ProviderStatus> {
    return request<ProviderStatus[]>('GET', '/providers').then(
      (list) => list.find((p) => p.provider === provider) ?? Promise.reject(new Error(`Provider ${provider} not found`))
    )
  },
}
