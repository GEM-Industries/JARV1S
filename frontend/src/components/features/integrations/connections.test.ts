import { describe, expect, it } from 'vitest'
import type { IntegrationSummary } from '../../../types'
import {
  COMPOSIO_CONNECTOR_LABEL,
  configModeLabel,
  connectionIds,
  connectionLabel,
  connectionSummary,
  isConnectionLinked,
  joinLabels,
  linkedConnectionIds,
} from './connections'

function item(overrides: Partial<IntegrationSummary> = {}): IntegrationSummary {
  return {
    name: 'calendar',
    display_name: 'Calendar',
    connected: true,
    loaded: true,
    tool_count: 8,
    status: 'connected',
    kind: 'built_in',
    enabled: true,
    description: 'Calendar',
    connection: 'connected',
    health: 'healthy',
    capabilities: [],
    auth_providers: ['macos', 'google', 'microsoft'],
    connected_providers: ['macos', 'google'],
    ...overrides,
  }
}

describe('connection helpers', () => {
  it('labels known connections without plugin-specific UI', () => {
    expect(connectionLabel('macos')).toBe('On this Mac')
    expect(connectionLabel('spotify')).toBe('Spotify')
    expect(configModeLabel('product')).toBe('Direct')
    expect(configModeLabel('self_managed')).toBe('Advanced')
    expect(configModeLabel(null)).toBeNull()
    expect(COMPOSIO_CONNECTOR_LABEL).toBe('Cloud connector — powered by Composio')
    expect(joinLabels(['On this Mac', 'Google'])).toBe('On this Mac and Google')
    expect(joinLabels(['On this Mac', 'Google', 'Microsoft'])).toBe(
      'On this Mac, Google, and Microsoft',
    )
  })

  it('treats connections as additive', () => {
    const calendar = item()
    const oauth = {
      google: { provider: 'google', connectable: true, connected: true, account_email: 'a@b.c' },
      microsoft: { provider: 'microsoft', connectable: false, connected: false, account_email: null },
    }
    expect(connectionIds(calendar)).toEqual(['macos', 'google', 'microsoft'])
    expect(isConnectionLinked('macos', calendar, oauth)).toBe(true)
    expect(isConnectionLinked('google', calendar, oauth)).toBe(true)
    expect(isConnectionLinked('microsoft', calendar, oauth)).toBe(false)
    expect(linkedConnectionIds(calendar, oauth)).toEqual(['macos', 'google'])
    expect(connectionSummary(calendar, oauth)).toBe('Connected via On this Mac and Google')
  })

  it('lists available sources when none are linked', () => {
    const calendar = item({
      connected: false,
      connected_providers: [],
      status: 'error',
      connection: 'disconnected',
      health: 'degraded',
    })
    expect(connectionSummary(calendar, {})).toBe('On this Mac, Google, and Microsoft')
  })
})
