import React, { useState } from 'react'
import { CheckIcon, CopyIcon, SpinnerIcon } from '@phosphor-icons/react'
import { Button } from '../../ui/Button'
import { PAIRING_FALLBACK_HINT, pairingExpiryLabel } from './pairing'

interface PairingCommandProps {
  command: string
  expiresAt: string
  hint?: string
  onRenew?: () => void
  renewing?: boolean
}

export const PairingCommand: React.FC<PairingCommandProps> = ({
  command,
  expiresAt,
  hint = PAIRING_FALLBACK_HINT,
  onRenew,
  renewing = false,
}) => {
  const [now, setNow] = useState(Date.now())
  const [copied, setCopied] = useState(false)
  const expired = new Date(expiresAt).getTime() <= now

  React.useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [])

  const copy = async () => {
    await navigator.clipboard.writeText(command)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="flex flex-col gap-2">
      {expired ? (
        <>
          <p className="type-body text-status-warning" role="status">
            Setup code expired
          </p>
          {onRenew && (
            <Button
              size="xs"
              color="brand"
              className="self-start"
              disabled={renewing}
              icon={renewing ? <SpinnerIcon className="animate-spin" size={12} /> : undefined}
              onClick={onRenew}
            >
              {renewing ? 'Connecting…' : 'Try again'}
            </Button>
          )}
        </>
      ) : (
        <>
          <p className="type-body text-foreground-muted">{hint}</p>
          <button
            type="button"
            onClick={() => void copy()}
            className="flex min-h-10 w-full items-center gap-3 rounded-control bg-canvas/40 p-3 text-left transition-colors duration-feedback hover:bg-canvas/55 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70"
          >
            <pre className="min-w-0 flex-1 overflow-x-auto font-mono type-meta text-foreground-muted">
              {command}
            </pre>
            <span className="inline-flex shrink-0 items-center gap-1.5 type-label-small text-brand">
              {copied ? <CheckIcon size={14} /> : <CopyIcon size={14} />}
              {copied ? 'Copied' : 'Copy'}
            </span>
          </button>
          <p className="type-meta text-foreground-subtle" role="status">
            {pairingExpiryLabel(expiresAt, now, 'Setup code expired')}
          </p>
        </>
      )}
    </div>
  )
}
