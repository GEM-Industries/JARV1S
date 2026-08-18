/**
 * Shared authenticated fetch for product REST APIs.
 *
 * Browser authentication uses the same-origin HttpOnly device cookie.
 * Pairing remains the unauthenticated bootstrap endpoint.
 */
import { useJarvisStore } from '../store/useJarvisStore'

export class ApiAuthError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiAuthError'
    this.status = status
  }
}

export async function authorizedFetch(
  input: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers)
  const response = await fetch(input, { ...init, headers })
  if (response.status === 401) {
    useJarvisStore.getState().setDevicePairingRequired(true)
  }
  return response
}

export async function authorizedJson<T>(
  input: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers({
    'Content-Type': 'application/json',
    ...(init.headers || {}),
  })
  const res = await authorizedFetch(input, { ...init, headers })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      detail = typeof json.detail === 'string' ? json.detail : detail
    } catch {
      // ignore
    }
    if (res.status === 401) {
      throw new ApiAuthError(detail, 401)
    }
    throw new Error(detail)
  }
  if (res.status === 204) {
    return undefined as T
  }
  return res.json() as Promise<T>
}
