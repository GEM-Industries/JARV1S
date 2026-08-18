import React, { useCallback, useEffect, useState } from 'react'
import {
  ArrowSquareOutIcon,
  ShieldCheckIcon,
} from '@phosphor-icons/react'
import {
  disableHostFunnel,
  enableHostFunnel,
  enableHostServe,
  getHostPrefs,
  getHostStatus,
  setHostPrefs,
  type HostPrefs,
  type HostReachabilityStatus,
} from '../../../runtime/desktopBridge'
import { ingressApi, type ExternalIngressState } from '../../../client/ingressApi'
import { openExternalUrl } from '../../../utils/openExternalUrl'
import { cn } from '../../../utils/cn'
import { isDesktopApp } from '../../../runtime/clientSurface'
import { DevicePairingCard } from '../presence/DevicePairingCard'
import { Button } from '../../ui/Button'
import { Disclosure } from '../../ui/Disclosure'
import { StatusPill } from '../../ui/StatusPill'
import { Switch } from '../../ui/Switch'

type ExternalTriggerStatus =
  | 'off'
  | 'configured'
  | 'verified'
  | 'needs_attention'
  | 'checking'

type TailscaleApproval = 'private_access' | 'external_triggers'

interface HostSettingsProps {
  section?: 'all' | 'access' | 'updates' | 'startup'
  embedded?: boolean
  onAccessChange?: () => void
}

function deriveExternalStatus(
  prefs: HostPrefs | null,
  status: HostReachabilityStatus | null,
  ingress: ExternalIngressState | null,
): ExternalTriggerStatus {
  if (!prefs || !status) return 'checking'
  const enabled =
    prefs.external_triggers_enabled || ingress?.enabled || status.funnel_configured
  if (!enabled) return 'off'

  if (
    ingress?.last_error ||
    (ingress?.inbox_dead_letter ?? 0) > 0 ||
    status.tailscale !== 'connected' ||
    !status.funnel_configured
  ) {
    return 'needs_attention'
  }

  if (ingress?.last_received_at) return 'verified'
  return 'configured'
}

export const HostSettings: React.FC<HostSettingsProps> = ({
  section = 'all',
  embedded = false,
  onAccessChange,
}) => {
  const desktop = isDesktopApp()
  const [status, setStatus] = useState<HostReachabilityStatus | null>(null)
  const [prefs, setPrefs] = useState<HostPrefs | null>(null)
  const [ingress, setIngress] = useState<ExternalIngressState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [enablingServe, setEnablingServe] = useState(false)
  const [togglingTriggers, setTogglingTriggers] = useState(false)
  const [approval, setApproval] = useState<{
    kind: TailscaleApproval
    url: string
  } | null>(null)

  const refresh = useCallback(async () => {
    if (!desktop) return
    try {
      const [nextStatus, nextPrefs, nextIngress] = await Promise.all([
        getHostStatus(),
        getHostPrefs(),
        ingressApi.get().catch(() => null),
      ])
      setStatus(nextStatus)
      setPrefs(nextPrefs)
      setIngress(nextIngress)
      setStatusError(null)
    } catch (err) {
      setStatusError(err instanceof Error ? err.message : 'Could not load host status')
    }
  }, [desktop])

  useEffect(() => {
    void refresh()
    if (!desktop) return
    const id = window.setInterval(() => void refresh(), 15000)
    return () => window.clearInterval(id)
  }, [desktop, refresh])

  if (!desktop) {
    if (section === 'updates') return null
    return (
      <div className="space-y-3 px-6 py-5">
        <p className="text-sm text-foreground-muted">
          Private access is managed on the Mac running JARV1S. This device connects to that Mac
          securely but cannot change its host settings.
        </p>
      </div>
    )
  }

  const updatePrefs = async (patch: Partial<HostPrefs>) => {
    if (!prefs) return
    setBusy(true)
    try {
      const next = await setHostPrefs({ ...prefs, ...patch })
      if (next) setPrefs(next)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save host preferences')
    } finally {
      setBusy(false)
    }
  }

  const enablePrivateAccess = async () => {
    setEnablingServe(true)
    setError(null)
    try {
      const result = await enableHostServe()
      if (!result) {
        setError('Private access controls are only available in the desktop app.')
        return
      }
      if (result.needs_consent && result.consent_url) {
        setApproval({ kind: 'private_access', url: result.consent_url })
        await openExternalUrl(result.consent_url)
        return
      }
      if (result.ok) {
        setApproval(null)
        await refresh()
        onAccessChange?.()
        return
      }
      setError(result.detail || 'Could not enable private access')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not enable private access')
    } finally {
      setEnablingServe(false)
    }
  }

  const enableExternalTriggers = async () => {
    setTogglingTriggers(true)
    setError(null)
    try {
      const result = await enableHostFunnel()
      if (!result) {
        setError('External triggers are only available in the desktop app.')
        return
      }
      if (result.needs_consent && result.consent_url) {
        setApproval({ kind: 'external_triggers', url: result.consent_url })
        await openExternalUrl(result.consent_url)
        return
      }
      if (result.ok) {
        setApproval(null)
        await refresh()
        return
      }
      setError(result.detail || 'Could not enable external triggers')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not enable external triggers')
    } finally {
      setTogglingTriggers(false)
    }
  }

  const disableExternalTriggers = async () => {
    setTogglingTriggers(true)
    setError(null)
    try {
      const result = await disableHostFunnel()
      if (!result) {
        setError('External triggers are only available in the desktop app.')
        return
      }
      if (!result.ok) {
        setError(result.detail || 'Could not disable external triggers')
      }
      setApproval(null)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not disable external triggers')
    } finally {
      setTogglingTriggers(false)
    }
  }

  const privateReady = status?.remote_healthy === true
  const privateSetup = (() => {
    if (!status) {
      return {
        title: 'Checking private access…',
        detail: 'JARV1S is checking whether this Mac can be reached from your other devices.',
        action: null as 'install' | 'signin' | 'enable' | 'retry' | null,
      }
    }
    if (privateReady) return null
    if (status.tailscale === 'not_installed') {
      return {
        title: 'Install secure access',
        detail:
          'Install Tailscale so phones and room speakers can reach this Mac privately.',
        action: 'install' as const,
      }
    }
    if (status.tailscale === 'offline') {
      return {
        title: 'Finish signing in',
        detail: 'Open Tailscale and sign in on this Mac, then check again.',
        action: 'signin' as const,
      }
    }
    if (status.tailscale === 'connected' && !status.serve_url) {
      return {
        title: 'Enable private access',
        detail: 'Share JARV1S with your approved devices over your private Tailscale network.',
        action: 'enable' as const,
      }
    }
    return {
      title: 'Private access needs attention',
      detail: 'JARV1S could not confirm that other devices can reach this Mac.',
      action: 'retry' as const,
    }
  })()

  const externalStatus = deriveExternalStatus(prefs, status, ingress)
  const externalPill = (() => {
    switch (externalStatus) {
      case 'verified':
        return { tone: 'success' as const, label: 'Working' }
      case 'configured':
        return { tone: 'active' as const, label: 'Ready' }
      case 'needs_attention':
        return { tone: 'warning' as const, label: 'Needs attention' }
      case 'checking':
        return { tone: 'neutral' as const, label: 'Checking…' }
      default:
        return { tone: 'neutral' as const, label: 'Off' }
    }
  })()
  const canEnableExternal = status?.tailscale === 'connected'
  const externalEnabled = Boolean(
    prefs?.external_triggers_enabled || ingress?.enabled || status?.funnel_configured,
  )
  const externalNeedsRepair =
    externalEnabled &&
    status?.tailscale === 'connected' &&
    (!status.funnel_configured || Boolean(ingress?.last_error))

  return (
    <div className={cn('space-y-5', !embedded && 'px-6 py-5')}>
      {(section === 'all' || section === 'access') && (
        <section className="rounded-panel border border-outline/20 bg-surface/20 p-4">
        <div className="flex items-start gap-3">
          <ShieldCheckIcon size={18} className="mt-0.5 shrink-0 text-brand" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <h3 className="type-heading text-foreground">Private access</h3>
              <StatusPill tone={privateReady ? 'success' : 'warning'}>
                {status
                  ? privateReady
                    ? 'Ready'
                    : 'Setup needed'
                  : 'Checking…'}
              </StatusPill>
            </div>
            <p className="mt-1 type-body text-foreground-muted">
              {privateReady
                ? 'Ready for phones and room speakers over your private Tailscale network.'
                : 'Connect phones and room speakers without exposing this Mac to the public internet.'}
            </p>
            {!privateReady && (
              <div className="mt-3 flex flex-wrap gap-2">
                <StatusPill tone={status?.backend_healthy ? 'success' : 'warning'}>
                  {status ? (status.backend_healthy ? 'JARV1S running' : 'JARV1S unavailable') : 'Checking JARV1S'}
                </StatusPill>
                {status?.tailscale === 'connected' && <StatusPill tone="success">Tailscale connected</StatusPill>}
              </div>
            )}
            {status?.sleep_risk && (
              <p className="mt-2 type-meta text-status-warning">
                Keep this Mac awake. Sleep pauses alarms, automations, and remote access.
              </p>
            )}
            {privateReady && status?.serve_url && (
              <Disclosure
                label="Connection details"
                className="mt-3 border-t border-outline/15 pt-1"
                contentClassName="pb-1 pl-5 type-meta text-foreground-subtle"
              >
                <p className="break-all font-mono">{status.serve_url}</p>
                <p className="mt-1">Only devices approved in your Tailscale network can use this address.</p>
              </Disclosure>
            )}
          </div>
        </div>
        </section>
      )}

      {(error || statusError) && (
        <p role="alert" className="rounded-control bg-status-danger/10 px-3 py-2 text-xs text-status-danger">
          {error || statusError}
        </p>
      )}

      {(section === 'all' || section === 'access') && privateSetup && (
        <section className="space-y-3 rounded-panel border border-outline/20 bg-surface/20 p-4">
          <div>
            <p className="mb-1 text-[10px] uppercase tracking-[0.18em] text-foreground-subtle">Next step</p>
            <h3 className="type-heading text-foreground">{privateSetup.title}</h3>
            <p className="mt-1 type-body text-foreground-muted">{privateSetup.detail}</p>
          </div>
          {approval?.kind === 'private_access' && (
            <div role="status" className="rounded-control bg-brand/10 p-3">
              <p className="text-xs font-medium text-foreground">Approve access in Tailscale</p>
              <p className="mt-1 text-xs text-foreground-muted">
                A Tailscale page opened. Approve this Mac, then return here to finish setup.
              </p>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            {privateSetup.action === 'install' && (
              <Button
                size="sm"
                variant="default"
                color="brand"
                icon={<ArrowSquareOutIcon size={14} />}
                onClick={() => {
                  void openExternalUrl('https://tailscale.com/download/mac').catch((error) => {
                    console.error('Failed to open Tailscale download', error)
                  })
                }}
              >
                Install Tailscale
              </Button>
            )}
            {privateSetup.action === 'enable' && approval?.kind !== 'private_access' && (
              <Button
                size="sm"
                variant="default"
                color="brand"
                disabled={enablingServe}
                onClick={() => void enablePrivateAccess()}
              >
                {enablingServe ? 'Enabling…' : 'Enable private access'}
              </Button>
            )}
            {approval?.kind === 'private_access' && (
              <>
                <Button
                  size="sm"
                  variant="default"
                  color="brand"
                  disabled={enablingServe}
                  onClick={() => void enablePrivateAccess()}
                >
                  {enablingServe ? 'Checking…' : 'I approved it — finish setup'}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  color="action"
                  onClick={() => void openExternalUrl(approval.url)}
                >
                  Open approval page
                </Button>
              </>
            )}
            {privateSetup.action === 'signin' && (
              <Button size="sm" variant="default" color="brand" onClick={() => void refresh()}>
                Check again
              </Button>
            )}
            {privateSetup.action === 'retry' && approval?.kind !== 'private_access' && (
              <Button
                size="sm"
                variant="default"
                color="brand"
                disabled={enablingServe}
                onClick={() => void enablePrivateAccess()}
              >
                {enablingServe ? 'Repairing…' : 'Repair private access'}
              </Button>
            )}
            {(privateSetup.action === 'install' ||
              (privateSetup.action === 'enable' && approval?.kind !== 'private_access')) && (
              <Button size="sm" variant="ghost" color="action" onClick={() => void refresh()}>
                Check again
              </Button>
            )}
          </div>
          {status?.detail && (
            <Disclosure
              label="Connection details"
              contentClassName="pb-1 pl-5 type-meta text-foreground-subtle"
            >
              <p>{status.detail}</p>
            </Disclosure>
          )}
        </section>
      )}

      {(section === 'all' || section === 'access') && (
        <DevicePairingCard
          disabled={!privateReady}
          disabledReason={
            !privateReady
              ? 'Set up private access on this Mac before pairing. You will also need Tailscale connected on your phone.'
              : undefined
          }
        />
      )}

      {(section === 'all' || section === 'updates') && (
        <section className="space-y-3 rounded-panel border border-outline/20 bg-surface/20 p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <h3 className="type-heading text-foreground">Instant service updates</h3>
            <p className="mt-1 type-body text-foreground-muted">
              Let connected services notify JARV1S as soon as something happens. This is optional;
              scheduled checks still work while it is off.
            </p>
          </div>
          <StatusPill tone={externalPill.tone}>{externalPill.label}</StatusPill>
        </div>
        {externalStatus === 'configured' && (
          <p className="mt-1 text-xs text-foreground-muted">
            Setup is complete. JARV1S will confirm it is working when the first update arrives.
          </p>
        )}
        {externalStatus === 'verified' && (
          <p className="text-xs text-foreground-muted">
            Connected services are successfully sending updates to this Mac.
          </p>
        )}
        {externalStatus === 'needs_attention' && status?.tailscale !== 'connected' && (
          <p className="text-xs text-status-warning">
            Tailscale is disconnected. Sign in to Tailscale on this Mac, then check again.
          </p>
        )}
        {externalNeedsRepair && (
          <p className="text-xs text-status-warning">
            The secure callback is incomplete. Repair setup to restore instant updates.
          </p>
        )}
        {ingress?.last_error && (
          <p className="text-xs text-status-danger">{ingress.last_error}</p>
        )}
        {(ingress?.inbox_dead_letter ?? 0) > 0 && (
          <p className="text-xs text-status-warning">
            {ingress?.inbox_dead_letter} failed event
            {(ingress?.inbox_dead_letter ?? 0) === 1 ? '' : 's'} need retry in Activity.
          </p>
        )}
        {approval?.kind === 'external_triggers' && (
          <div role="status" className="rounded-control bg-brand/10 p-3">
            <p className="text-xs font-medium text-foreground">Approve instant updates in Tailscale</p>
            <p className="mt-1 text-xs text-foreground-muted">
              A Tailscale page opened. Approve Funnel for this Mac, then return here to finish setup.
            </p>
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          {approval?.kind === 'external_triggers' ? (
            <>
              <Button
                size="sm"
                variant="default"
                color="brand"
                disabled={togglingTriggers}
                onClick={() => void enableExternalTriggers()}
              >
                {togglingTriggers ? 'Checking…' : 'I approved it — finish setup'}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                color="action"
                onClick={() => void openExternalUrl(approval.url)}
              >
                Open approval page
              </Button>
            </>
          ) : !externalEnabled || externalNeedsRepair ? (
            <Button
              size="sm"
              variant="default"
              color="brand"
              disabled={!canEnableExternal || togglingTriggers}
              onClick={() => void enableExternalTriggers()}
            >
              {togglingTriggers
                ? externalNeedsRepair
                  ? 'Repairing…'
                  : 'Turning on…'
                : externalNeedsRepair
                  ? 'Repair setup'
                  : 'Turn on instant updates'}
            </Button>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              color="action"
              disabled={togglingTriggers}
              onClick={() => void disableExternalTriggers()}
            >
              {togglingTriggers ? 'Turning off…' : 'Turn off'}
            </Button>
          )}
        </div>
        {!canEnableExternal && status?.tailscale !== 'connected' && (
          <p className="text-xs text-foreground-subtle">
            Set up private access in Home → Devices before turning on instant updates.
          </p>
        )}
        {(status?.funnel_url || ingress?.base_url) && (
          <Disclosure
            label="Technical details"
            contentClassName="pb-1 pl-5 type-meta text-foreground-subtle"
          >
            <p>
              Tailscale Funnel exposes only JARV1S webhook and push callback paths. Requests must still
              pass provider signature or token checks.
            </p>
            <p className="mt-1 break-all font-mono">{ingress?.base_url || status?.funnel_url}</p>
          </Disclosure>
        )}
        </section>
      )}

      {(section === 'all' || section === 'startup') && (
        <section className="space-y-2 rounded-panel border border-outline/20 bg-surface/20 p-4">
        <div>
          <h3 className="type-heading text-foreground">Startup behavior</h3>
          <p className="mt-1 type-body text-foreground-muted">
            Start JARV1S automatically and keep it running when its window is closed.
          </p>
        </div>
        <Switch
          checked={Boolean(prefs?.launch_at_login)}
          disabled={busy || !prefs}
          onChange={(checked) => void updatePrefs({ launch_at_login: checked })}
          label="Launch at login"
        />
        <Switch
          checked={Boolean(prefs?.hide_on_close)}
          disabled={busy || !prefs}
          onChange={(checked) => void updatePrefs({ hide_on_close: checked })}
          label="Keep running when the window closes"
        />
        </section>
      )}

    </div>
  )
}
