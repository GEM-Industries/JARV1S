import React, { useEffect, useRef, useState } from 'react';
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  ArrowSquareOutIcon,
  CheckIcon,
  CopySimpleIcon,
  CheckCircleIcon,
  LockKeyIcon,
  WarningIcon,
  XCircleIcon,
} from '@phosphor-icons/react';
import { cn } from '../../../utils/cn';
import { Button } from '../../ui/Button';
import { OAuthApiError, oauthApi } from '../../../client/oauthApi';
import { isDesktopApp } from '../../../runtime/clientSurface';
import { WidgetDefinition, BaseWidgetProps } from './types';
import {
  beginOAuthAuthorization,
  closeOAuthPopup,
  watchOAuthCompletion,
} from '../../../utils/oauthFlow';

// ---------------------------------------------------------------------------
// Per-provider static config (deep-links, display names, field visibility)
// ---------------------------------------------------------------------------

interface SetupStep {
  title: string
  description: string
  url: string
  cta: string
  substeps?: string[]
  showRedirectUri?: boolean
}

const PROVIDER_CONFIG = {
  google: {
    displayName: 'Google',
    requiresSecret: true,
    secretLabel: 'Client Secret',
    setupSteps: [
      {
        title: 'Create a Cloud project',
        description: 'Open Google Cloud Console and create a new project for JARV1S.',
        url: 'https://console.cloud.google.com/projectcreate',
        cta: 'Open Cloud Console',
      },
      {
        title: 'Configure consent screen',
        description: 'Set an app name, choose "External" audience, and save.',
        url: 'https://console.cloud.google.com/auth/branding',
        cta: 'Open Auth Platform',
      },
      {
        title: 'Publish the app',
        description: 'Go to Audience and click "Publish App". Without this, tokens expire every 7 days.',
        url: 'https://console.cloud.google.com/auth/audience',
        cta: 'Open Audience settings',
      },
      {
        title: 'Enable APIs',
        description: 'Enable the Google APIs that JARV1S will use. Click each link and hit "Enable".',
        url: 'https://console.cloud.google.com/apis/library',
        cta: 'Open API Library',
        substeps: [
          'Enable Google Calendar API',
          'Enable Gmail API',
        ],
      },
      {
        title: 'Create OAuth credentials',
        description: 'Create a new OAuth client and add the redirect URI below.',
        url: 'https://console.cloud.google.com/auth/clients/create',
        cta: 'Open Clients page',
        substeps: [
          'Application type → "Web application"',
          'Add the Authorized redirect URI below',
          'Click Create, then copy the Client ID & Secret',
        ],
        showRedirectUri: true,
      },
    ] satisfies SetupStep[],
  },
  microsoft: {
    displayName: 'Microsoft',
    requiresSecret: false,
    secretLabel: null,
    setupSteps: [
      {
        title: 'Register an app',
        description: 'In Azure Portal, register a new app. Select "Accounts in any org + personal".',
        url: 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/CreateApplicationBlade',
        cta: 'Open Azure Portal',
      } satisfies SetupStep,
      {
        title: 'Enable public client flows',
        description: 'Go to Authentication → Advanced settings → toggle "Allow public client flows" On.',
        url: 'https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade',
        cta: 'Open App registrations',
      } satisfies SetupStep,
    ],
  },
} satisfies Record<string, { displayName: string; requiresSecret: boolean; secretLabel: string | null; setupSteps: SetupStep[] }>

type Provider = keyof typeof PROVIDER_CONFIG

type OAuthPhase = 'connected' | 'error'

interface OAuthWidgetData {
  provider: string
  phase?: OAuthPhase
  missing_scopes?: string[]
  account_email?: string
}

// ---------------------------------------------------------------------------
// Progress dots
// ---------------------------------------------------------------------------

const StepDots: React.FC<{ total: number; current: number }> = ({ total, current }) => (
  <div className="flex items-center gap-1.5">
    {Array.from({ length: total }, (_, i) => (
      <div
        key={i}
        className={cn(
          'h-1 rounded-full transition-all duration-200',
          i < current ? 'w-1 bg-status-success/60' :
          i === current ? 'w-4 bg-brand' :
          'w-1 bg-outline/30'
        )}
      />
    ))}
  </div>
)

// ---------------------------------------------------------------------------
// Copyable redirect URI
// ---------------------------------------------------------------------------

const CopyableUri: React.FC = () => {
  const uri = `${window.location.origin}/api/v1/auth/oauth/callback`
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    await navigator.clipboard.writeText(uri)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <button
      type="button"
      onClick={handleCopy}
      className={cn(
        'flex w-full items-center gap-2 rounded-control px-3 py-2 text-left',
        'bg-canvas-sunken/60 border border-outline/20',
        'hover:border-brand/30 transition-colors duration-150 group/copy'
      )}
    >
      <span className="text-[10px] font-mono text-foreground-muted truncate flex-1">
        {uri}
      </span>
      {copied ? (
        <CheckIcon size={12} weight="bold" className="text-status-success shrink-0" />
      ) : (
        <CopySimpleIcon size={12} weight="bold" className="text-foreground-disabled/40 group-hover/copy:text-brand/60 shrink-0 transition-colors" />
      )}
    </button>
  )
}

// ---------------------------------------------------------------------------
// Advanced self-managed OAuth setup.
// ---------------------------------------------------------------------------

const NeedsConfigPhase: React.FC<{
  provider: Provider
  onConfigured: () => void
}> = ({ provider, onConfigured }) => {
  const cfg = PROVIDER_CONFIG[provider]
  const steps = cfg.setupSteps
  const totalSteps = steps.length

  const [stepIdx, setStepIdx] = useState(0)
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const atCredentials = stepIdx >= totalSteps
  const step: SetupStep = steps[Math.min(stepIdx, totalSteps - 1)]

  const handleSubmit = async () => {
    if (!clientId.trim()) return
    setSubmitting(true)
    setError(null)
    try {
      await oauthApi.configure(
        provider,
        clientId.trim(),
        cfg.requiresSecret ? clientSecret.trim() || undefined : undefined
      )
      onConfigured()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Configuration failed.')
    } finally {
      setSubmitting(false)
    }
  }

  const handleNext = () => {
    if (stepIdx < totalSteps - 1) {
      setStepIdx((s) => s + 1)
    } else if (!atCredentials) {
      setStepIdx(totalSteps)
    }
  }

  if (atCredentials) {
    return (
      <div className="flex flex-col h-full px-5 py-4 select-none gap-4">
        <StepDots total={totalSteps + 1} current={totalSteps} />

        <div>
          <p className="font-body text-sm text-foreground leading-snug mb-1">
            Enter your credentials
          </p>
          <p className="font-body text-xs text-foreground-muted leading-relaxed">
            Paste the {cfg.requiresSecret ? 'Client ID and Secret' : 'Application (client) ID'} from {cfg.displayName}.
          </p>
        </div>

        <div className="flex flex-col gap-2 mt-auto">
          <input
            type="text"
            placeholder="Client ID"
            value={clientId}
            onChange={(e) => setClientId(e.target.value)}
            autoFocus
            className={cn(
              'w-full h-10 px-3 text-xs font-mono text-foreground placeholder:text-foreground-disabled/30',
              'bg-canvas-sunken/60 rounded-control border border-outline/20',
              'outline-none focus:border-brand/30 transition-colors duration-150'
            )}
          />
          {cfg.requiresSecret && (
            <input
              type="password"
              placeholder={cfg.secretLabel ?? 'Client Secret'}
              value={clientSecret}
              onChange={(e) => setClientSecret(e.target.value)}
              className={cn(
                'w-full h-10 px-3 text-xs font-mono text-foreground placeholder:text-foreground-disabled/30',
                'bg-canvas-sunken/60 rounded-control border border-outline/20',
                'outline-none focus:border-brand/30 transition-colors duration-150'
              )}
            />
          )}

          {error && (
            <div className="flex items-start gap-2">
              <WarningIcon size={12} className="text-status-danger shrink-0 mt-0.5" />
              <p className="text-[10px] font-body text-status-danger leading-snug">{error}</p>
            </div>
          )}

          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              color="subtle"
              size="sm"
              onClick={() => setStepIdx(totalSteps - 1)}
              className="shrink-0"
              icon={<ArrowLeftIcon size={14} weight="bold" />}
            >
              Back
            </Button>
            <Button
              variant="ghost"
              color="brand"
              size="sm"
              onClick={handleSubmit}
              disabled={submitting || !clientId.trim()}
              className="flex-1"
              icon={<LockKeyIcon size={14} weight="bold" />}
            >
              {submitting ? 'Saving\u2026' : 'Save credentials'}
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full px-5 py-4 select-none gap-4">
      <StepDots total={totalSteps + 1} current={stepIdx} />

      <div className="flex-1 flex flex-col justify-center gap-3 overflow-y-auto min-h-0">
        <p className="font-body text-sm text-foreground leading-snug">
          {step.title}
        </p>
        <p className="font-body text-xs text-foreground-muted leading-relaxed">
          {step.description}
        </p>

        {step.substeps && (
          <ol className="flex flex-col gap-1">
            {step.substeps.map((sub, i) => (
              <li key={i} className="flex items-start gap-2">
                <span className="text-[10px] font-mono text-foreground-disabled/50 w-3 shrink-0 pt-px">{i + 1}.</span>
                <span className="text-[11px] font-body text-foreground-muted leading-snug">{sub}</span>
              </li>
            ))}
          </ol>
        )}

        {step.showRedirectUri && <CopyableUri />}

        <a
          href={step.url}
          target="_blank"
          rel="noopener noreferrer"
          className={cn(
            'inline-flex items-center gap-1.5 text-xs font-mono text-brand/80',
            'hover:text-brand transition-colors duration-150'
          )}
        >
          {step.cta}
          <ArrowSquareOutIcon size={12} weight="bold" className="opacity-60" />
        </a>
      </div>

      <div className="flex items-center gap-2 shrink-0">
        {stepIdx > 0 && (
          <Button
            variant="ghost"
            color="subtle"
            size="sm"
            onClick={() => setStepIdx((s) => s - 1)}
            className="shrink-0"
            icon={<ArrowLeftIcon size={14} weight="bold" />}
          >
            Back
          </Button>
        )}
        <Button
          variant="ghost"
          color="brand"
          size="sm"
          onClick={handleNext}
          className="flex-1"
          icon={<ArrowRightIcon size={14} weight="bold" />}
        >
          {stepIdx < totalSteps - 1 ? 'Done, next step' : 'Done, enter credentials'}
        </Button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Primary connect path.
// ---------------------------------------------------------------------------

const ConnectPhase: React.FC<{
  provider: Provider
  missingScopes?: string[]
  onConnected: (email: string) => void
  onAdvancedSetup: () => void
}> = ({ provider, missingScopes, onConnected, onAdvancedSetup }) => {
  const cfg = PROVIDER_CONFIG[provider]
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const popupRef = useRef<Window | null>(null)
  const cleanupRef = useRef<(() => void) | null>(null)

  useEffect(
    () => () => {
      cleanupRef.current?.()
      closeOAuthPopup(popupRef.current)
    },
    []
  )

  const handleAuthorize = async () => {
    setBusy(true)
    setError(null)

    try {
      const { authorize_url } = await oauthApi.authorize(provider, window.location.origin)
      const launch = await beginOAuthAuthorization(`Connect ${cfg.displayName}`, authorize_url)
      popupRef.current = launch.popup ?? null

      cleanupRef.current = watchOAuthCompletion({
        app: provider,
        mode: launch.mode,
        popup: launch.popup,
        checkComplete: () => oauthApi.getProviderStatus(provider).then((s) => s.connected),
        onComplete: (msg) => {
          cleanupRef.current = null
          popupRef.current = null
          if (msg.success) {
            oauthApi.getProviderStatus(provider)
              .then((s) => onConnected(s.account_email ?? ''))
              .catch(() => onConnected(''))
          } else {
            setBusy(false)
            setError('Authorization failed. Please try again.')
          }
        },
        onAborted: () => {
          cleanupRef.current = null
          popupRef.current = null
          setBusy(false)
          setError(
            launch.mode === 'external'
              ? 'Authorization timed out. Finish sign-in in your browser, then try again.'
              : 'Authorization window closed before completing.'
          )
        },
      })
    } catch (e) {
      closeOAuthPopup(popupRef.current)
      popupRef.current = null
      setBusy(false)
      if (e instanceof OAuthApiError && e.status === 409) {
        onAdvancedSetup()
        return
      }
      setError(e instanceof Error ? e.message : 'Could not start authorization.')
    }
  }

  return (
    <div className="flex flex-col h-full px-5 py-4 select-none gap-3">
      <div className="flex flex-col gap-2">
        <p className="font-body text-sm text-foreground leading-snug">
          Authorize {cfg.displayName}
        </p>
        {missingScopes && missingScopes.length > 0 ? (
          <p className="font-body text-xs text-foreground-muted leading-relaxed">
            Additional permissions needed:{' '}
            <span className="font-mono text-brand/80">
              {missingScopes.map((s) => s.split('/').pop()).join(', ')}
            </span>
          </p>
        ) : (
          <p className="font-body text-xs text-foreground-muted leading-relaxed">
            {busy
              ? (isDesktopApp()
                ? 'Sign in in your browser, then return to JARV1S.'
                : 'Complete sign-in in the browser window, then return here.')
              : (isDesktopApp()
                ? 'Your default browser will open to sign in and grant permissions.'
                : `A browser window will open to sign in with ${cfg.displayName} and grant permissions.`)}
          </p>
        )}
        {provider === 'google' && (
          <p className="text-[10px] font-mono text-foreground-disabled/50 leading-relaxed">
            You may see &quot;Google hasn&apos;t verified this app&quot; — click Advanced to continue. This is normal for personal apps.
          </p>
        )}
      </div>

      {error && (
        <div className="flex items-start gap-2">
          <XCircleIcon size={12} className="text-status-danger shrink-0 mt-0.5" />
          <p className="text-[10px] font-body text-status-danger leading-snug">{error}</p>
        </div>
      )}

      <Button
        variant="ghost"
        color="brand"
        size="sm"
        onClick={handleAuthorize}
        disabled={busy}
        className="mt-auto w-full"
        icon={<ArrowSquareOutIcon size={14} weight="bold" />}
      >
        {busy ? `Waiting for ${cfg.displayName}\u2026` : `Authorize ${cfg.displayName}`}
      </Button>

      <button
        type="button"
        onClick={onAdvancedSetup}
        className="text-[10px] font-mono text-foreground-disabled/50 hover:text-foreground-subtle transition-colors text-center"
      >
        Use your own OAuth app instead
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Phase: connected
// ---------------------------------------------------------------------------

const ConnectedPhase: React.FC<{
  provider: Provider
  email: string
  onDisconnect: () => void
}> = ({ provider, email, onDisconnect }) => {
  const cfg = PROVIDER_CONFIG[provider]
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)

  const handleDisconnect = async () => {
    setBusy(true)
    try {
      await oauthApi.deleteProvider(provider)
      onDisconnect()
    } catch {
      setBusy(false)
      setConfirming(false)
    }
  }

  return (
    <div className="flex flex-col items-center justify-center h-full px-5 py-5 gap-3 select-none">
      <CheckCircleIcon size={36} weight="fill" className="text-status-success" />
      <p className="font-body text-base text-foreground text-center leading-snug">
        {cfg.displayName} connected
      </p>
      {email && (
        <p className="font-mono text-xs text-foreground-muted">{email}</p>
      )}
      <div className="mt-2">
        {confirming ? (
          <div className="flex items-center gap-2">
            <Button variant="ghost" color="subtle" size="sm" onClick={() => setConfirming(false)} disabled={busy}>
              Cancel
            </Button>
            <Button variant="ghost" color="critical" size="sm" onClick={handleDisconnect} disabled={busy}>
              {busy ? 'Removing\u2026' : 'Confirm disconnect'}
            </Button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="text-[10px] font-mono text-foreground-disabled/40 hover:text-status-danger/60 transition-colors"
          >
            Disconnect
          </button>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main hero component
// ---------------------------------------------------------------------------

const OAuthHero: React.FC<OAuthWidgetData & BaseWidgetProps> = ({
  provider: providerRaw,
  phase,
  missing_scopes,
  account_email,
}) => {
  const provider = providerRaw as Provider
  const cfg = PROVIDER_CONFIG[provider]
  const [showAdvancedSetup, setShowAdvancedSetup] = useState(false)
  const [connectedEmail, setConnectedEmail] = useState(account_email ?? '')
  const isConnected = phase === 'connected' || Boolean(connectedEmail)
  const isError = phase === 'error'

  useEffect(() => {
    setConnectedEmail(account_email ?? '')
  }, [account_email])

  useEffect(() => {
    if (isConnected || showAdvancedSetup) return

    let cancelled = false
    void oauthApi.getProviderStatus(provider)
      .then((status) => {
        if (!cancelled && !status.connectable) {
          setShowAdvancedSetup(true)
        }
      })
      .catch(() => {
        // If status lookup fails, keep the normal connect path and show any authorize error there.
      })

    return () => {
      cancelled = true
    }
  }, [isConnected, provider, showAdvancedSetup])

  const handleConfigured = () => setShowAdvancedSetup(false)

  const handleDisconnected = () => {
    setConnectedEmail('')
    setShowAdvancedSetup(false)
  }

  const handleConnected = (email: string) => {
    setConnectedEmail(email)
    setShowAdvancedSetup(false)
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 pt-4 pb-2 shrink-0">
        <LockKeyIcon
          size={16}
          weight="fill"
          className={cn(
            isConnected ? 'text-status-success' :
            isError ? 'text-status-danger' :
            'text-brand'
          )}
        />
        <span className="type-heading text-foreground">
          {cfg?.displayName ?? provider} OAuth
        </span>
        {isConnected && (
          <span className="ml-auto type-label-small text-status-success">
            Connected
          </span>
        )}
      </div>

      <div className="flex-1 overflow-hidden">
        {showAdvancedSetup ? (
          <NeedsConfigPhase provider={provider} onConfigured={handleConfigured} />
        ) : isConnected ? (
          <ConnectedPhase provider={provider} email={connectedEmail} onDisconnect={handleDisconnected} />
        ) : (
          <ConnectPhase
            provider={provider}
            missingScopes={missing_scopes}
            onConnected={handleConnected}
            onAdvancedSetup={() => setShowAdvancedSetup(true)}
          />
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Widget definition
// ---------------------------------------------------------------------------

export const OAuthWidget: WidgetDefinition<OAuthWidgetData> = {
  Hero: OAuthHero,
  getCompressedConfig: (data) => ({
    icon: (
      <LockKeyIcon
        size={20}
        weight="fill"
        className={cn(
          data.phase === 'connected' ? 'text-status-success' :
          data.phase === 'error' ? 'text-status-danger' :
          'text-brand'
        )}
      />
    ),
    label: data.phase === 'connected' || data.account_email ? 'Connected' : 'Auth',
    labelVariant: 'mono',
    width: 'wide',
  }),
}
