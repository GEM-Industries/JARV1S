import type { ProviderStatus } from '../../../client/oauthApi'
import type { IntegrationSummary } from '../../../types'

const CONNECTION_LABELS: Record<string, string> = {
  google: 'Google',
  microsoft: 'Microsoft',
  macos: 'On this Mac',
  spotify: 'Spotify',
}

export function oauthRedirectUri(provider: string): string {
  let origin = window.location.origin
  if (provider === 'spotify') {
    origin = origin.replace(/^http:\/\/localhost\b/i, 'http://127.0.0.1')
  }
  return `${origin}/api/v1/auth/oauth/callback`
}

export const COMPOSIO_CONNECTOR_LABEL = 'Cloud connector — powered by Composio'

export function configModeLabel(
  mode: ProviderStatus['config_mode'],
): 'Direct' | 'Advanced' | null {
  if (mode === 'product') return 'Direct'
  if (mode === 'self_managed') return 'Advanced'
  return null
}

export function connectionLabel(id: string): string {
  return CONNECTION_LABELS[id] ?? id.replace(/[_-]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

export function joinLabels(labels: string[]): string {
  if (labels.length === 0) return ''
  if (labels.length === 1) return labels[0]
  if (labels.length === 2) return `${labels[0]} and ${labels[1]}`
  return `${labels.slice(0, -1).join(', ')}, and ${labels[labels.length - 1]}`
}

/** Connection ids a plugin can link. Empty for Composio-only apps. */
export function connectionIds(item: IntegrationSummary): string[] {
  if (item.auth_providers?.length) return item.auth_providers
  if (item.auth_type && item.auth_type !== 'composio') return [item.auth_type]
  return []
}

export function isConnectionLinked(
  id: string,
  item: IntegrationSummary,
  oauth: Record<string, ProviderStatus>,
): boolean {
  if (item.connected_providers?.includes(id)) return true
  return oauth[id]?.connected ?? false
}

export function linkedConnectionIds(
  item: IntegrationSummary,
  oauth: Record<string, ProviderStatus>,
): string[] {
  return connectionIds(item).filter((id) => isConnectionLinked(id, item, oauth))
}

/** List-row subtitle for a multi-connection plugin. */
export function connectionSummary(
  item: IntegrationSummary,
  oauth: Record<string, ProviderStatus>,
): string | null {
  const ids = connectionIds(item)
  if (!ids.length) return null
  const linked = linkedConnectionIds(item, oauth)
  if (linked.length) {
    return `Connected via ${joinLabels(linked.map(connectionLabel))}`
  }
  return joinLabels(ids.map(connectionLabel))
}
