import React, { useEffect, useRef, useState } from 'react'
import { DeviceMobileIcon, ShieldCheckIcon } from '@phosphor-icons/react'
import { jarvisClient } from '../../client/JarvisClient'
import { isPhoneCompanion } from '../../runtime/clientSurface'
import { useJarvisStore } from '../../store/useJarvisStore'
import { Button } from '../ui/Button'

function readPairCodeFromUrl(): string {
  const params = new URLSearchParams(window.location.search)
  return formatPairCode(params.get('pair') || '')
}

function formatPairCode(value: string): string {
  const compact = value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 6)
  return compact.length > 3 ? `${compact.slice(0, 3)}-${compact.slice(3)}` : compact
}

function clearPairCodeFromUrl(): void {
  const url = new URL(window.location.href)
  if (!url.searchParams.has('pair')) return
  url.searchParams.delete('pair')
  window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
}

export const DevicePairingBanner: React.FC = () => {
  const needsPairing = useJarvisStore((s) => s.devicePairingRequired)
  const phone = isPhoneCompanion()
  const [code, setCode] = useState(() => readPairCodeFromUrl())
  const [openedFromLink] = useState(() => Boolean(readPairCodeFromUrl()))
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const autoAttempted = useRef(false)

  useEffect(() => {
    if (phone || !needsPairing || autoAttempted.current) return
    const fromUrl = readPairCodeFromUrl()
    if (!fromUrl) return
    autoAttempted.current = true
    setCode(fromUrl)
    setBusy(true)
    setError(null)
    void jarvisClient
      .pairWithCode(fromUrl)
      .then(() => {
        clearPairCodeFromUrl()
        setCode('')
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : 'Pairing failed')
      })
      .finally(() => setBusy(false))
  }, [needsPairing, phone])

  if (!needsPairing) return null

  const handlePair = async () => {
    setBusy(true)
    setError(null)
    try {
      await jarvisClient.pairWithCode(code.trim().toUpperCase())
      clearPairCodeFromUrl()
      setCode('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Pairing failed')
    } finally {
      setBusy(false)
    }
  }

  if (phone) {
    const host = window.location.hostname
    return (
      <div className="fixed inset-0 z-[100] overflow-y-auto bg-canvas px-6 py-10">
        <div className="mx-auto w-full max-w-sm space-y-6">
          <div className="flex h-14 w-14 items-center justify-center rounded-panel bg-brand/15 text-brand">
            <DeviceMobileIcon size={28} />
          </div>
          <div>
            <p className="type-fui text-brand">
              Private companion
            </p>
            <h1 className="mt-2 type-title text-foreground">Connect to {host}</h1>
            <p className="mt-3 type-body text-foreground-muted">
              This phone connects to your JARV1S Mac over your private network. Your Host controls
              how requests are processed.
            </p>
          </div>
          <div className="flex items-start gap-3 rounded-control bg-surface/30 p-4">
            <ShieldCheckIcon size={20} className="mt-0.5 shrink-0 text-status-success" />
            <p className="type-meta text-foreground-muted">
              Microphone access is requested only when you first hold the Talk button.
            </p>
          </div>
          <form
            className="space-y-6"
            onSubmit={(event) => {
              event.preventDefault()
              void handlePair()
            }}
          >
            <div>
              {openedFromLink && !error ? (
                <div className="flex items-start gap-3 rounded-control bg-status-success/10 p-4">
                  <ShieldCheckIcon size={20} className="mt-0.5 shrink-0 text-status-success" />
                  <div>
                    <p className="type-label text-foreground">Pairing link received</p>
                    <p className="mt-1 type-meta text-foreground-muted">
                      Confirm that this is your JARV1S Mac, then connect.
                    </p>
                  </div>
                </div>
              ) : (
                <>
                  <label htmlFor="phone-pair-code" className="type-label-small text-foreground-muted">
                    6-character pairing code
                  </label>
                  <input
                    id="phone-pair-code"
                    className="mt-2 min-h-12 w-full rounded-control border border-outline bg-surface/30 px-4 font-mono text-body tracking-[0.18em] text-foreground"
                    placeholder="ABC-234"
                    value={code}
                    maxLength={7}
                    autoCapitalize="characters"
                    autoComplete="one-time-code"
                    spellCheck={false}
                    aria-describedby={error ? 'phone-pair-error' : undefined}
                    onChange={(event) => setCode(formatPairCode(event.target.value))}
                    disabled={busy}
                  />
                </>
              )}
            </div>
            <Button
              type="submit"
              size="md"
              color="brand"
              className="min-h-12 w-full"
              disabled={busy || !code.trim()}
            >
              {busy ? 'Connecting…' : 'Connect this phone'}
            </Button>
            {error && (
              <p id="phone-pair-error" role="alert" className="type-body text-status-danger">
                {error}
              </p>
            )}
          </form>
        </div>
      </div>
    )
  }

  return (
    <div className="pointer-events-auto mx-auto mt-16 max-w-md rounded-panel border border-brand/30 bg-canvas/95 p-4 shadow-lg backdrop-blur">
      <h2 className="type-heading text-foreground">Pair this device</h2>
      <p className="mt-1 type-meta text-foreground-muted">
        Enter a pairing code from Home → Devices, or open a shared pairing link.
      </p>
      <label htmlFor="device-pair-code" className="mt-3 block type-label-small text-foreground-muted">
        Pairing code
      </label>
      <div className="mt-2 flex gap-2">
        <input
          id="device-pair-code"
          className="min-h-10 flex-1 rounded-control border border-outline-subtle bg-canvas px-3 py-2 font-mono text-body text-foreground"
          placeholder="ABC-234"
          value={code}
          maxLength={7}
          autoCapitalize="characters"
          autoComplete="one-time-code"
          spellCheck={false}
          onChange={(e) => setCode(formatPairCode(e.target.value))}
          disabled={busy}
        />
        <Button
          size="sm"
          color="brand"
          onClick={() => void handlePair()}
          disabled={busy || !code.trim()}
        >
          Pair
        </Button>
      </div>
      {error ? <p className="mt-2 text-xs text-status-danger">{error}</p> : null}
    </div>
  )
}
