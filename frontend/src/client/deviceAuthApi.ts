import { authorizedJson } from './http'

export interface PairDeviceRequest {
  code: string
  node_id: string
  node_label?: string
  capabilities?: string
  client_surface?: 'browser' | 'desktop_app' | 'phone' | 'satellite'
  room_id?: string
  room_name?: string
  location_provider?: string
  ha_area_id?: string
  ha_device_id?: string
  ha_entity_id?: string
}

export interface PairDeviceResponse {
  device_id: string
  owner_id: string
  node_id: string
}

export interface WsTicketResponse {
  ticket: string
  expires_at: string
}

export async function pairDevice(body: PairDeviceRequest): Promise<PairDeviceResponse> {
  const res = await fetch('/api/v1/device-auth/pair', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(typeof detail.detail === 'string' ? detail.detail : 'Pairing failed')
  }
  return res.json() as Promise<PairDeviceResponse>
}

export async function mintWsTicket(deviceToken?: string): Promise<WsTicketResponse> {
  const res = await fetch('/api/v1/device-auth/ws-ticket', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(deviceToken ? { device_token: deviceToken } : {}),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}))
    throw new Error(typeof detail.detail === 'string' ? detail.detail : 'Ticket request failed')
  }
  return res.json() as Promise<WsTicketResponse>
}

export interface IssuePairingCodeRequest {
  node_label?: string
  capabilities?: string[]
  room_name?: string
  node_id?: string
  ha_area_id?: string
}

export interface IssuePairingCodeResponse {
  code: string
  expires_at: string
  owner_id: string
  pairing_url?: string | null
}

export async function issuePairingCode(
  body: IssuePairingCodeRequest = {},
): Promise<IssuePairingCodeResponse> {
  return authorizedJson<IssuePairingCodeResponse>('/api/v1/device-auth/pairing-codes', {
    method: 'POST',
    body: JSON.stringify(body),
  })
}
