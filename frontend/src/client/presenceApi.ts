/**
 * REST client for presence visibility API.
 */

import { authorizedFetch } from './http'

const API_BASE = '/api/v1/presence'

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
      // ignore
    }
    throw new Error(detail)
  }

  return res.json() as Promise<T>
}

export type PresenceNodeStatus = 'online' | 'offline'
export type DeviceKind = 'browser' | 'desktop' | 'phone' | 'satellite' | 'unknown'

export interface PresenceCore {
  name: string
}

export interface PresenceNode {
  node_id: string
  node_label?: string | null
  kind: DeviceKind
  status: PresenceNodeStatus
  capabilities: string[]
  room_name?: string | null
  ha_area_id?: string | null
  last_seen_at?: string | null
  active: boolean
  device_id?: string | null
  disconnected: boolean
}

export interface PresenceView {
  core: PresenceCore
  nodes: PresenceNode[]
}

export interface RevokeDeviceResponse {
  revoked: boolean
}

export interface DisconnectDeviceResponse {
  disconnected: boolean
}

export interface ResumeDeviceResponse {
  resumed: boolean
}

export const presenceApi = {
  getPresence: () => request<PresenceView>('GET', '/'),
  assignNodeRoom: (nodeId: string, haAreaId: string | null) =>
    request<PresenceView>('PATCH', `/nodes/${encodeURIComponent(nodeId)}/room`, {
      ha_area_id: haAreaId,
    }),
  revokeDevice: (deviceId: string) =>
    request<RevokeDeviceResponse>('POST', `/devices/${encodeURIComponent(deviceId)}/revoke`),
  disconnectDevice: (deviceId: string) =>
    request<DisconnectDeviceResponse>('POST', `/devices/${encodeURIComponent(deviceId)}/disconnect`),
  resumeDevice: (deviceId: string) =>
    request<ResumeDeviceResponse>('POST', `/devices/${encodeURIComponent(deviceId)}/resume`),
}
