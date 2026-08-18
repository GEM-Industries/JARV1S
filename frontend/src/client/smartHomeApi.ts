/**
 * REST client for Home Assistant visibility API.
 */

import { authorizedFetch } from './http'

const API_BASE = '/api/v1/smart-home'

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

export type SmartHomeUiStatus =
  | 'unconfigured'
  | 'invalid_config'
  | 'unreachable'
  | 'auth_failed'
  | 'registry_unavailable'
  | 'empty_inventory'
  | 'ready'

export interface DeviceSummary {
  entity_id: string
  name: string
  domain: string
  state: string
  area_name?: string | null
  brightness_pct?: number | null
  color_temp_kelvin?: number | null
  capabilities: string[]
}

export interface SmartHomeStatusResponse {
  status: SmartHomeUiStatus
  message: string
  next_action?: string | null
  ha_url?: string | null
  configured: boolean
  reachable: boolean
  authenticated: boolean
  registry_access: boolean
  ready: boolean
  area_count: number
  device_count: number
  safe_controllable_count: number
  devices: DeviceSummary[]
  devices_truncated: boolean
}

export interface BoundRoomNode {
  node_id: string
  node_label?: string | null
  device_id?: string | null
  kind: 'browser' | 'phone' | 'satellite'
  room_name?: string | null
}

export interface RoomSummary {
  area_id: string
  name: string
  exists_in_ha: boolean
  device_count: number
  entity_count: number
  bound_nodes: BoundRoomNode[]
}

export interface RoomsResponse {
  rooms: RoomSummary[]
}

export interface RoomMutationResponse {
  room?: RoomSummary | null
  rooms: RoomSummary[]
  affected_node_ids: string[]
  cleared_node_ids: string[]
}

export interface HaConnectRequest {
  url: string
  token: string
}

export interface HaDiscoverResponse {
  found: boolean
  url?: string | null
}

export interface HaAuthorizeResponse {
  authorize_url: string
  ha_url: string
}

export const smartHomeApi = {
  getStatus: () => request<SmartHomeStatusResponse>('GET', '/status'),
  discover: () => request<HaDiscoverResponse>('GET', '/discover'),
  authorize: (url: string, origin: string) =>
    request<HaAuthorizeResponse>('POST', '/auth/authorize', { url, origin }),
  connect: (payload: HaConnectRequest) =>
    request<SmartHomeStatusResponse>('POST', '/connect', payload),
  disconnect: () => request<SmartHomeStatusResponse>('DELETE', '/connect'),
  getRooms: () => request<RoomsResponse>('GET', '/rooms'),
  createRoom: (name: string) => request<RoomMutationResponse>('POST', '/rooms', { name }),
  renameRoom: (areaId: string, name: string) =>
    request<RoomMutationResponse>('PATCH', `/rooms/${encodeURIComponent(areaId)}`, { name }),
  deleteRoom: (areaId: string) =>
    request<RoomMutationResponse>('DELETE', `/rooms/${encodeURIComponent(areaId)}`),
}
