import React, { useEffect, useState } from 'react'
import {
  CheckCircleIcon,
  CopyIcon,
  DeviceMobileIcon,
  QrCodeIcon,
} from '@phosphor-icons/react'
import { issuePairingCode } from '../../../client/deviceAuthApi'
import { getHostStatus } from '../../../runtime/desktopBridge'
import { isDesktopApp } from '../../../runtime/clientSurface'
import { Button } from '../../ui/Button'
import { Disclosure } from '../../ui/Disclosure'

interface IssuedPairing {
  code: string
  expires_at: string
  pairing_url: string
}

function buildPairingUrl(code: string, remoteBase?: string | null): string {
  const origin = remoteBase?.replace(/\/$/, '') || window.location.origin
  const url = new URL('/', origin)
  url.searchParams.set('pair', code)
  url.searchParams.set('jarvis_surface', 'phone')
  return url.toString()
}

function expiryLabel(expiresAt: string, now: number): string {
  const remainingSeconds = Math.max(0, Math.ceil((new Date(expiresAt).getTime() - now) / 1000))
  if (remainingSeconds === 0) return 'Pairing code expired'
  const minutes = Math.floor(remainingSeconds / 60)
  const seconds = String(remainingSeconds % 60).padStart(2, '0')
  return `Expires in ${minutes}:${seconds}`
}

interface DevicePairingCardProps {
  disabled?: boolean
  disabledReason?: string
  onOpenPrivateAccess?: () => void
}

export const DevicePairingCard: React.FC<DevicePairingCardProps> = ({
  disabled = false,
  disabledReason,
  onOpenPrivateAccess,
}) => {
  const [issued, setIssued] = useState<IssuedPairing | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [qrSrc, setQrSrc] = useState<string | null>(null)
  const [qrFailed, setQrFailed] = useState(false)
  const [privateAccessRequired, setPrivateAccessRequired] = useState(false)
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    let active = true
    if (!issued) {
      setQrSrc(null)
      setQrFailed(false)
      return
    }
    setQrFailed(false)
    void import('qrcode')
      .then(({ default: QRCode }) =>
        QRCode.toDataURL(issued.pairing_url, { width: 180, margin: 1 }),
      )
      .then((url) => {
        if (active) setQrSrc(url)
      })
      .catch(() => {
        if (active) setQrFailed(true)
      })
    return () => {
      active = false
    }
  }, [issued])

  useEffect(() => {
    if (!issued) return
    setNow(Date.now())
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [issued])

  const issue = async () => {
    setBusy(true)
    setError(null)
    setCopied(false)
    setPrivateAccessRequired(false)
    try {
      let remoteBase: string | null = null
      if (isDesktopApp()) {
        const status = await getHostStatus()
        if (status?.remote_healthy !== true || !status.serve_url) {
          setPrivateAccessRequired(true)
          throw new Error('Private access is not ready. Finish setup above, then try again.')
        }
        remoteBase = status?.serve_url ?? null
      }
      const result = await issuePairingCode()
      const pairing_url = buildPairingUrl(result.code, remoteBase)
      setIssued({
        code: result.code,
        expires_at: result.expires_at,
        pairing_url,
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not issue pairing code')
    } finally {
      setBusy(false)
    }
  }

  const copy = async () => {
    if (!issued) return
    await navigator.clipboard.writeText(issued.pairing_url)
    setCopied(true)
  }

  const expired = issued ? new Date(issued.expires_at).getTime() <= now : false

  return (
    <section className="flex flex-col gap-4 overflow-hidden rounded-panel bg-surface/20 p-4">
      <div className="flex items-start gap-3">
        <DeviceMobileIcon size={18} className="mt-0.5 shrink-0 text-brand" />
        <div className="min-w-0 flex-1">
          <h3 className="type-label text-foreground">Connect your phone</h3>
          <p className="mt-0.5 type-meta text-foreground-subtle">
            Pair once, then talk to JARV1S from your phone.
          </p>
        </div>
      </div>

      {(disabledReason || error) && (
        <div className="flex flex-col gap-2 overflow-hidden rounded-control bg-status-warning/10 px-3 py-3">
          <p className="type-body text-status-warning">{error || disabledReason}</p>
          {onOpenPrivateAccess && (Boolean(disabledReason) || privateAccessRequired) && (
            <Button
              size="xs"
              variant="ghost"
              color="brand"
              className="self-start"
              onClick={onOpenPrivateAccess}
            >
              Review private access
            </Button>
          )}
        </div>
      )}

      {!issued ? (
        <div className="flex flex-col gap-4">
          <p className="type-body text-foreground-muted">
            First connect Tailscale on your phone using the same account as this Mac. Then scan a
            one-time QR code—no code entry required.
          </p>
          <Button
            size="sm"
            color="brand"
            className="self-start"
            icon={<QrCodeIcon size={16} />}
            disabled={busy || disabled}
            onClick={() => void issue()}
          >
            {busy ? 'Preparing…' : 'Pair a phone'}
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="flex items-start gap-3">
            <CheckCircleIcon size={18} className="mt-0.5 shrink-0 text-status-success" />
            <div className="min-w-0">
              <p className="type-label text-foreground">Scan with your phone</p>
              <p className="mt-0.5 type-body text-foreground-muted">
                Open Camera, scan the QR code, then confirm the connection on your phone.
              </p>
            </div>
          </div>
          {qrSrc ? (
            <img
              src={qrSrc}
              alt="Scan to connect your phone to JARV1S"
              width={200}
              height={200}
              className="mx-auto overflow-hidden rounded-control border border-outline/20 bg-white p-2"
            />
          ) : qrFailed ? (
            <p className="py-4 text-center type-body text-status-warning">
              QR unavailable. Copy the pairing link instead.
            </p>
          ) : (
            <p role="status" className="py-8 text-center type-body text-foreground-muted">
              Preparing QR…
            </p>
          )}
          <p
            className={`text-center type-meta ${expired ? 'text-status-warning' : 'text-foreground-subtle'}`}
            role="status"
          >
            {expiryLabel(issued.expires_at, now)}
          </p>

          {expired ? (
            <Button className="self-center" size="sm" color="brand" onClick={() => void issue()}>
              Create a new QR code
            </Button>
          ) : (
            <Disclosure
              label="Can’t scan the QR code?"
              variant="surface"
              contentClassName="flex flex-col items-center gap-3 pb-3 pt-2 text-center"
            >
                <div>
                  <p className="type-meta text-foreground-subtle">
                    Open JARV1S on your phone and enter this code
                  </p>
                  <p className="mt-2 font-mono text-xl tracking-[0.24em] text-foreground">
                    {issued.code}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  color="brand"
                  icon={<CopyIcon size={14} />}
                  onClick={() => void copy()}
                >
                  {copied ? 'Pairing link copied' : 'Copy pairing link'}
                </Button>
            </Disclosure>
          )}
        </div>
      )}
    </section>
  )
}
