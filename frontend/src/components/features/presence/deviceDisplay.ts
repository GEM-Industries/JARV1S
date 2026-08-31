import type { DeviceKind, PresenceNode } from '../../../client/presenceApi'
import type { HostReachabilityStatus, SpeakerReachability } from '../../../runtime/desktopBridge'
import type { LanPairStatus } from './pairing'

export type PrivateAccess = 'unknown' | 'ready' | 'needs_setup'
export type SpeakerDiagnosis = { checking: boolean; message: string | null }
export type SpeakerReconnect = {
  command: string
  expiresAt: string
  lanStatus: LanPairStatus
}

export function labelForKind(kind: DeviceKind): string {
  switch (kind) {
    case 'browser':
      return 'Browser'
    case 'desktop':
      return 'Desktop app'
    case 'phone':
      return 'Phone'
    case 'satellite':
      return 'Room speaker'
    default:
      return 'Device'
  }
}

export function titleCase(value: string): string {
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

export function displayNameForNode(node: PresenceNode, thisDevice: boolean): string {
  const explicit = node.node_label?.trim()
  if (explicit) return explicit
  if (thisDevice && (node.kind === 'browser' || node.kind === 'desktop')) return 'This Mac'
  if (thisDevice && node.kind === 'phone') return 'This phone'
  if (node.kind === 'browser') return 'Browser'
  if (node.kind === 'desktop') return 'Desktop app'
  if (node.kind === 'phone') return 'Phone'
  if (node.kind === 'satellite') return titleCase(node.node_id)
  return titleCase(node.node_id) || 'Device'
}

export function toneForStatus(node: PresenceNode): 'success' | 'warning' | 'off' {
  if (node.disconnected) return 'off'
  return node.status === 'online' ? 'success' : 'warning'
}

export function statusLabel(node: PresenceNode): string {
  if (node.disconnected) return 'Disconnected'
  return node.status === 'online' ? 'Online' : 'Offline'
}

export function labelForCapability(cap: string): string {
  switch (cap) {
    case 'mic':
      return 'Mic'
    case 'speaker':
      return 'Speaker'
    case 'display':
      return 'Display'
    default:
      return titleCase(cap)
  }
}

export function formatLastSeen(iso?: string | null): string | null {
  if (!iso) return null
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  const diffMs = Date.now() - date.getTime()
  if (diffMs < 60_000) return 'just now'
  const mins = Math.floor(diffMs / 60_000)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

export function contextForNode(node: PresenceNode, privateAccess: PrivateAccess): string | null {
  if (node.disconnected) return 'Still paired'
  if (node.status === 'online') return null
  const lastSeen = formatLastSeen(node.last_seen_at)
  if (node.kind === 'satellite') {
    if (privateAccess === 'needs_setup') {
      return lastSeen
        ? `Last seen ${lastSeen} · turn on private access on this Mac`
        : 'Offline · turn on private access on this Mac'
    }
    if (privateAccess === 'ready') {
      return lastSeen
        ? `Last seen ${lastSeen} · speaker is not reaching this Mac`
        : 'Offline · speaker is not reaching this Mac'
    }
    return lastSeen
      ? `Last seen ${lastSeen} · check private access and restart the speaker`
      : 'Offline · check private access and restart the speaker'
  }
  return lastSeen ? `Last seen ${lastSeen} · reconnects when reopened` : 'Offline · reconnects when reopened'
}

export function speakerDiagnosisMessage(
  host: HostReachabilityStatus | null,
  speaker: SpeakerReachability | null,
): string {
  if (!host?.backend_healthy) {
    return 'JARV1S on this Mac is not running — restart it, then check again.'
  }
  if (host.remote_healthy !== true) {
    return 'This Mac’s private access is not working — review setup below, then restart the speaker.'
  }
  if (!speaker || speaker.network === 'unavailable') {
    return 'Could not check the speaker — Tailscale is not responding on this Mac.'
  }
  if (speaker.network === 'not_found' || speaker.network === 'offline') {
    const seen = speaker.network === 'offline' ? formatLastSeen(speaker.last_seen) : null
    return `This Mac is fine — the speaker is not on your private network${seen ? ` (last seen ${seen})` : ''}. Check that it is powered on and connected to Wi-Fi.`
  }
  return 'This Mac is fine and the speaker is on the network, but its JARV1S service is not connecting. Restart the speaker; if it stays offline, reconnect it from here.'
}

export function groupNodes(nodes: PresenceNode[]): { title: string; nodes: PresenceNode[] }[] {
  const online = nodes.filter((n) => n.status === 'online')
  const offline = nodes.filter((n) => n.status === 'offline')
  const groups: { title: string; nodes: PresenceNode[] }[] = []
  if (online.length) groups.push({ title: 'Online', nodes: online })
  if (offline.length) groups.push({ title: 'Offline', nodes: offline })
  return groups
}
