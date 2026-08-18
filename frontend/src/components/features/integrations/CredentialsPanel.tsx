import React, { useCallback, useEffect, useState } from 'react'
import { SpinnerIcon, TrashIcon, WarningIcon } from '@phosphor-icons/react'
import {
  credentialsApi,
  type CredentialCard,
  type CredentialsListResponse,
} from '../../../client/credentialsApi'
import { Button } from '../../ui/Button'
import { EmptyState } from '../../ui/EmptyState'
import { Input } from '../../ui/FieldControl'
import { PanelSection } from '../../ui/PanelSection'
import { StatusPill } from '../../ui/StatusPill'
import { ModelSwitcherCard } from './ModelSwitcherCard'

interface CredentialsPanelProps {
  active: boolean
  section?: 'all' | 'model' | 'credentials'
}

interface CredentialRowProps {
  card: CredentialCard
  onChanged: (card: CredentialCard) => void
}

const CredentialRow: React.FC<CredentialRowProps> = ({ card, onChanged }) => {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [confirmRemove, setConfirmRemove] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const stored = card.status === 'stored'
  const needsMigration = card.status === 'env_deprecated'
  const statusLabel = stored ? 'Connected' : needsMigration ? 'Move key' : 'Not connected'
  const statusTone = stored ? 'success' : needsMigration ? 'warning' : 'neutral'

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await credentialsApi.save(card.id, value)
      onChanged(result.card)
      setConfirmRemove(false)
      setValue('')
      setEditing(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save credential.')
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await credentialsApi.remove(card.id)
      onChanged(result.card)
      setConfirmRemove(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove credential.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <PanelSection className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="type-heading text-foreground">{card.label}</h3>
          <p className="mt-1 type-body text-foreground-muted">{card.description}</p>
          {card.masked_suffix && <p className="mt-1 font-mono text-xs text-foreground-subtle">{card.masked_suffix}</p>}
        </div>
        <StatusPill tone={statusTone}>{statusLabel}</StatusPill>
      </div>
      <div>
          {editing ? (
            <div className="mt-4 space-y-3">
              <label htmlFor={`credential-${card.id}`} className="block type-label-small text-foreground-muted">API key</label>
              <Input id={`credential-${card.id}`} type="password" value={value} onChange={(event) => setValue(event.target.value)} autoComplete="off" spellCheck={false} invalid={Boolean(error)} />
              <div className="flex gap-2">
                <Button size="sm" disabled={busy || !value.trim()} onClick={() => void save()}>Save key</Button>
                <Button size="sm" variant="ghost" color="neutral" disabled={busy} onClick={() => setEditing(false)}>Cancel</Button>
              </div>
            </div>
          ) : (
            <div className="mt-4 flex gap-2">
              <Button size="sm" onClick={() => setEditing(true)}>
                {stored ? 'Replace key' : needsMigration ? 'Store securely' : 'Connect'}
              </Button>
              {stored && !confirmRemove && (
                <Button size="sm" variant="ghost" color="danger" icon={<TrashIcon size={14} />} disabled={busy} onClick={() => setConfirmRemove(true)}>Remove</Button>
              )}
              {confirmRemove && (
                <>
                  <Button size="sm" color="critical" disabled={busy} onClick={() => void remove()}>Confirm remove</Button>
                  <Button size="sm" variant="ghost" color="neutral" disabled={busy} onClick={() => setConfirmRemove(false)}>Cancel</Button>
                </>
              )}
            </div>
          )}
          {error && <p className="mt-2 flex items-center gap-2 type-body text-status-danger" role="alert"><WarningIcon size={14} />{error}</p>}
      </div>
    </PanelSection>
  )
}

export const CredentialsPanel: React.FC<CredentialsPanelProps> = ({ active, section = 'all' }) => {
  const [data, setData] = useState<CredentialsListResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setData(await credentialsApi.list())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load credentials.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (active && section !== 'model') void load()
  }, [active, load, section])

  if (!active) return null
  if (section === 'model') return <div className="px-6 py-5"><ModelSwitcherCard active={active} /></div>
  if (loading && !data) {
    return (
      <div className="space-y-4 px-6 py-5" aria-busy="true">
        <p className="flex items-center gap-2 type-body text-foreground-muted">
          <SpinnerIcon className="animate-spin" size={16} />
          Loading connections…
        </p>
        {Array.from({ length: 3 }).map((_, index) => (
          <PanelSection key={index} className="h-28 animate-pulse bg-surface/15" aria-hidden />
        ))}
      </div>
    )
  }
  if (error && !data) return <EmptyState className="m-5" tone="error" title="Could not load credentials" description={error} action={<Button size="sm" onClick={() => void load()}>Retry</Button>} />
  if (!data) return null

  const capabilityCards = data.items
  return (
    <div className="space-y-4 px-6 py-5">
      <p className="type-body text-foreground-muted">
        Connect optional services to extend what JARV1S can understand and do. Keys stay securely on this host.
      </p>
      {capabilityCards.map((card) => (
        <CredentialRow
          key={card.id}
          card={card}
          onChanged={(changed) => setData((current) => current ? {
            ...current,
            items: current.items.map((item) => item.id === changed.id ? changed : item),
          } : current)}
        />
      ))}
    </div>
  )
}
