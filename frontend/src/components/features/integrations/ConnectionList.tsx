import React from 'react'
import type { ProviderStatus } from '../../../client/oauthApi'
import type { IntegrationSummary } from '../../../types'
import { Button } from '../../ui/Button'
import { ActionMenu } from '../../ui/ActionMenu'
import { StatusPill } from '../../ui/StatusPill'
import {
  connectionIds,
  connectionLabel,
  configModeLabel,
  isConnectionLinked,
} from './connections'

interface ConnectionListProps {
  item: IntegrationSummary
  providerStatuses: Record<string, ProviderStatus>
  busyId?: string
  onConnect: (name: string, connectionId: string) => void
  onSetup: (name: string, connectionId: string) => void
  onDisconnect: (name: string, connectionId: string) => void
}

/**
 * Additive connections for a plugin (this Mac, Google, Microsoft, …).
 * One primary action per row; disconnect lives in the overflow.
 */
export const ConnectionList: React.FC<ConnectionListProps> = ({
  item,
  providerStatuses,
  busyId,
  onConnect,
  onSetup,
  onDisconnect,
}) => {
  const ids = connectionIds(item)
  if (!ids.length) return null

  return (
    <div>
      <p className="type-label-small text-foreground-subtle">Connections</p>
      <p className="mt-1 type-body text-foreground-muted">
        JARV1S uses every connected source.
      </p>
      <div className="ui-surface-group mt-3">
        {ids.map((id) => {
          const oauth = providerStatuses[id]
          const needsSetup = oauth ? !oauth.connectable : false
          const linked = isConnectionLinked(id, item, providerStatuses)
          const busy = busyId === id
          const tone = needsSetup ? 'error' : linked ? 'success' : 'off'
          const stateLabel = needsSetup ? 'Setup needed' : linked ? 'Connected' : 'Not connected'
          const actionLabel = needsSetup ? 'Set up' : busy ? 'Waiting…' : linked ? 'Reconnect' : 'Connect'
          const modeLabel = configModeLabel(oauth?.config_mode)

          return (
            <div key={id} className="flex min-h-14 items-center gap-3 bg-canvas-sunken/25 px-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <p className="type-label text-foreground">{connectionLabel(id)}</p>
                  {modeLabel ? (
                    <span className="type-meta text-foreground-subtle">{modeLabel}</span>
                  ) : null}
                  <StatusPill tone={tone}>{stateLabel}</StatusPill>
                </div>
                {oauth?.account_email ? (
                  <p className="mt-0.5 truncate type-meta text-foreground-subtle">
                    {oauth.account_email}
                  </p>
                ) : null}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                <Button
                  variant="ghost"
                  color="brand"
                  size="xs"
                  shape="control"
                  disabled={busy}
                  onClick={() => (needsSetup ? onSetup(item.name, id) : onConnect(item.name, id))}
                >
                  {actionLabel}
                </Button>
                {linked ? (
                  <ActionMenu label={`More actions for ${connectionLabel(id)}`}>
                    <ActionMenu.Item
                      tone="danger"
                      disabled={busy}
                      onClick={() => onDisconnect(item.name, id)}
                    >
                      Disconnect
                    </ActionMenu.Item>
                  </ActionMenu>
                ) : null}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
