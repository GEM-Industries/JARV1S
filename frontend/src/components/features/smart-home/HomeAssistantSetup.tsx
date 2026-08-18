import React, { useCallback, useEffect, useRef, useState } from 'react'
import { CaretLeftIcon, SpinnerIcon } from '@phosphor-icons/react'
import {
  smartHomeApi,
  type SmartHomeStatusResponse,
} from '../../../client/smartHomeApi'
import { openExternalUrl } from '../../../utils/openExternalUrl'
import {
  beginOAuthAuthorization,
  closeOAuthPopup,
  watchOAuthCompletion,
  type OAuthLaunchMode,
} from '../../../utils/oauthFlow'
import { Button } from '../../ui/Button'
import { Disclosure } from '../../ui/Disclosure'
import { FieldControl, Input } from '../../ui/FieldControl'
import { PanelSection } from '../../ui/PanelSection'
import { TextLink } from '../../ui/TextLink'
import {
  homeAssistantHost,
  normalizeHomeAssistantUrl,
  openHomeAssistant,
} from './homeAssistantUrl'

const HA_INSTALL_URL = 'https://www.home-assistant.io/installation/'
const HA_MACOS_INSTALL_URL = 'https://www.home-assistant.io/installation/macos/'
const HA_AUTH_APP = 'home_assistant'
const HA_SECURITY_PATH = '/profile/security'

type SetupPhase = 'discovering' | 'authorize' | 'install'

function errMsg(e: unknown, fb: string) {
  return e instanceof Error ? e.message : fb
}

function initialPhase(status?: SmartHomeStatusResponse | null): SetupPhase {
  if (status?.status === 'auth_failed' || status?.status === 'invalid_config') {
    return 'authorize'
  }
  return 'discovering'
}

export const HomeAssistantSetup: React.FC<{
  onConnected: (status: SmartHomeStatusResponse) => void
  initialStatus?: SmartHomeStatusResponse | null
}> = ({ onConnected, initialStatus }) => {
  const [phase, setPhase] = useState<SetupPhase>(() => initialPhase(initialStatus))
  const [haUrl, setHaUrl] = useState(initialStatus?.ha_url ?? '')
  const [manualUrl, setManualUrl] = useState(initialStatus?.ha_url ?? '')
  const [connectToken, setConnectToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [authorizing, setAuthorizing] = useState(false)
  const [authMode, setAuthMode] = useState<OAuthLaunchMode | null>(null)
  const popupRef = useRef<Window | null>(null)
  const cleanupRef = useRef<(() => void) | null>(null)
  const discoveryStarted = useRef(false)

  const clearAuthWatch = useCallback(() => {
    cleanupRef.current?.()
    cleanupRef.current = null
    closeOAuthPopup(popupRef.current)
    popupRef.current = null
  }, [])

  useEffect(() => () => clearAuthWatch(), [clearAuthWatch])

  const runDiscovery = useCallback(async () => {
    setPhase('discovering')
    setError(null)
    try {
      const result = await smartHomeApi.discover()
      if (result.found && result.url) {
        setHaUrl(result.url)
        setManualUrl(result.url)
        setPhase('authorize')
      } else {
        setHaUrl('')
        setPhase('authorize')
        setError('Could not find Home Assistant on this network. Enter its address below.')
      }
    } catch (e) {
      setPhase('authorize')
      setError(errMsg(e, 'Could not look for Home Assistant.'))
    }
  }, [])

  useEffect(() => {
    if (phase !== 'discovering' || discoveryStarted.current) return
    discoveryStarted.current = true
    void runDiscovery()
  }, [phase, runDiscovery])

  const startAuthorize = useCallback(async (url: string) => {
    const normalized = normalizeHomeAssistantUrl(url)
    if (!normalized) {
      setError('Enter a valid Home Assistant address.')
      return
    }
    setHaUrl(normalized)
    setManualUrl(normalized)
    setAuthorizing(true)
    setAuthMode(null)
    setError(null)
    clearAuthWatch()
    try {
      const { authorize_url, ha_url } = await smartHomeApi.authorize(
        normalized,
        window.location.origin,
      )
      setHaUrl(ha_url)
      setManualUrl(ha_url)
      const launch = await beginOAuthAuthorization('Connect Home Assistant', authorize_url, {
        openExternal: openExternalUrl,
      })
      setAuthMode(launch.mode)
      popupRef.current = launch.popup ?? null
      cleanupRef.current = watchOAuthCompletion({
        app: HA_AUTH_APP,
        mode: launch.mode,
        popup: launch.popup,
        checkComplete: async () => {
          const status = await smartHomeApi.getStatus()
          return status.configured && status.authenticated
        },
        onComplete: (message) => {
          clearAuthWatch()
          setAuthorizing(false)
          setAuthMode(null)
          if (!message.success) {
            setError('Authorization did not finish. Try again or connect manually.')
            return
          }
          void smartHomeApi
            .getStatus()
            .then((status) => onConnected(status))
            .catch((e) => setError(errMsg(e, 'Connected, but status could not be refreshed.')))
        },
        onAborted: () => {
          clearAuthWatch()
          setAuthorizing(false)
          setAuthMode(null)
          setError(
            launch.mode === 'external'
              ? 'Authorization timed out. Finish sign-in in your browser, then try again.'
              : 'Auth window closed before finishing.',
          )
        },
      })
    } catch (e) {
      setAuthorizing(false)
      setAuthMode(null)
      setError(errMsg(e, 'Could not start Home Assistant authorization.'))
    }
  }, [clearAuthWatch, onConnected])

  const handleManualConnect = useCallback(async () => {
    const url = (manualUrl || haUrl).trim()
    const token = connectToken.trim()
    if (!url || !token) {
      setError('Enter your Home Assistant URL and access token.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const status = await smartHomeApi.connect({ url, token })
      setConnectToken('')
      onConnected(status)
    } catch (e) {
      setError(errMsg(e, 'Could not connect to Home Assistant.'))
    } finally {
      setBusy(false)
    }
  }, [connectToken, haUrl, manualUrl, onConnected])

  if (phase === 'discovering') {
    return (
      <div
        className="flex min-h-40 flex-col items-center justify-center gap-3 rounded-panel bg-surface/[0.08] px-6 py-8 text-center"
        aria-live="polite"
      >
        <SpinnerIcon className="animate-spin text-foreground-subtle" size={20} />
        <p className="type-heading text-foreground">Finding Home Assistant…</p>
        <p className="max-w-md type-body text-foreground-muted">
          Looking for a Home Assistant instance on this network.
        </p>
      </div>
    )
  }

  if (phase === 'install') {
    return (
      <div className="flex flex-col gap-5">
        <div>
          <h3 className="type-heading text-foreground">Install Home Assistant</h3>
          <p className="mt-1 type-body text-foreground-muted">
            Use Home Assistant OS in a virtual machine on this Mac when it stays home, powered on,
            and awake. Prefer another always-on device if this Mac sleeps or leaves home.
          </p>
        </div>
        <ol className="list-decimal space-y-3 pl-5 marker:text-foreground-subtle type-body text-foreground-muted">
          <li className="pl-1">Open the official guide and install Home Assistant.</li>
          <li className="pl-1">
            Finish first-run setup in your browser at{' '}
            <span className="text-foreground">homeassistant.local:8123</span>.
          </li>
          <li className="pl-1">Return here and let JARV1S find and connect it.</li>
        </ol>
        <div className="flex flex-col gap-3">
          <Button
            color="brand"
            size="md"
            className="w-full"
            onClick={() => void openExternalUrl(HA_MACOS_INSTALL_URL)}
          >
            Open macOS install guide
          </Button>
          <Button color="subtle" size="md" className="w-full" onClick={() => void runDiscovery()}>
            I finished setup — find Home Assistant
          </Button>
          <div className="text-center">
            <TextLink external onClick={() => void openExternalUrl(HA_INSTALL_URL)}>
              See install options for other hardware
            </TextLink>
          </div>
        </div>
        <div>
          <Button
            variant="ghost"
            color="neutral"
            size="sm"
            icon={<CaretLeftIcon size={14} />}
            onClick={() => setPhase('authorize')}
          >
            Back to connection
          </Button>
        </div>
      </div>
    )
  }

  const resolvedUrl = (manualUrl || haUrl).trim()
  const foundHost = homeAssistantHost(haUrl)
  const connectUrl = foundHost ? haUrl : manualUrl

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h3 className="type-heading text-foreground">
          {foundHost ? 'Connect Home Assistant' : 'Enter Home Assistant address'}
        </h3>
        <p className="mt-1 type-body text-foreground-muted">
          {foundHost
            ? `Found ${foundHost}. Sign in to authorize JARV1S.`
            : 'Both this Mac and Home Assistant must be on the same network.'}
        </p>
      </div>

      {!foundHost && (
        <FieldControl
          label="Home Assistant URL"
          htmlFor="ha-setup-url"
          hint="Example: homeassistant.local:8123. Use 127.0.0.1:8123 only if Home Assistant runs on this Mac."
        >
          <Input
            id="ha-setup-url"
            type="url"
            value={manualUrl}
            onChange={(e) => setManualUrl(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder="homeassistant.local:8123"
            className="font-mono"
            invalid={Boolean(error)}
          />
        </FieldControl>
      )}

      {error && (
        <p className="type-body text-status-danger-fg" role="alert">
          {error}
        </p>
      )}

      {authorizing && authMode === 'external' && (
        <p className="type-body text-foreground-muted" aria-live="polite">
          Finish sign-in in your browser, then return here.
        </p>
      )}

      <div className="flex flex-col gap-3">
        <Button
          color="brand"
          size="md"
          className="w-full"
          disabled={authorizing || busy || !connectUrl.trim()}
          icon={authorizing ? <SpinnerIcon className="animate-spin" size={16} /> : undefined}
          onClick={() => void startAuthorize(connectUrl)}
        >
          {authorizing ? 'Waiting for authorization' : 'Sign in to connect'}
        </Button>
        {foundHost && (
          <Button
            variant="ghost"
            color="neutral"
            size="md"
            className="self-start"
            disabled={authorizing || busy}
            onClick={() => {
              setHaUrl('')
              setManualUrl(haUrl)
              setError(null)
            }}
          >
            Use a different address
          </Button>
        )}
      </div>

      <PanelSection
        as="section"
        className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"
      >
        <div className="flex min-w-0 flex-col gap-1">
          <h4 className="type-label text-foreground">New to Home Assistant?</h4>
          <p className="type-meta text-foreground-subtle">
            Install it first, then JARV1S can find it automatically.
          </p>
        </div>
        <Button
          variant="ghost"
          color="brand"
          size="sm"
          shape="control"
          className="shrink-0 self-start sm:self-auto"
          disabled={authorizing || busy}
          onClick={() => {
            clearAuthWatch()
            setAuthorizing(false)
            setAuthMode(null)
            setError(null)
            setPhase('install')
          }}
        >
          Set up Home Assistant
        </Button>
      </PanelSection>

      <Disclosure label="Advanced: connect with a token">
        <div className="mt-3 flex flex-col gap-4">
          {!foundHost && !manualUrl.trim() ? (
            <FieldControl
              label="Home Assistant URL"
              htmlFor="ha-manual-url"
              hint="Long-lived tokens only — refresh tokens from the login list will not work."
            >
              <Input
                id="ha-manual-url"
                type="url"
                value={manualUrl}
                onChange={(e) => setManualUrl(e.target.value)}
                autoComplete="off"
                spellCheck={false}
                placeholder="homeassistant.local:8123"
                className="font-mono"
              />
            </FieldControl>
          ) : (
            <p className="type-meta text-foreground-muted">
              Using <span className="text-foreground">{foundHost || resolvedUrl}</span>. Long-lived
              tokens only — refresh tokens from the login list will not work.
            </p>
          )}
          <FieldControl label="Long-lived access token" htmlFor="ha-manual-token">
            <Input
              id="ha-manual-token"
              type="password"
              value={connectToken}
              onChange={(e) => setConnectToken(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              className="font-mono"
              invalid={Boolean(error)}
            />
          </FieldControl>
          <ol className="list-decimal space-y-2 pl-5 marker:text-foreground-subtle type-meta text-foreground-muted">
            <li className="pl-1">
              Open{' '}
              <TextLink
                external
                disabled={!normalizeHomeAssistantUrl(resolvedUrl)}
                onClick={() => openHomeAssistant(resolvedUrl)}
              >
                Home Assistant
              </TextLink>
              , then{' '}
              <TextLink
                external
                disabled={!normalizeHomeAssistantUrl(resolvedUrl)}
                onClick={() => openHomeAssistant(resolvedUrl, HA_SECURITY_PATH)}
              >
                Security
              </TextLink>
              .
            </li>
            <li className="pl-1">
              Create a token named <span className="text-foreground">JARV1S</span>, copy it once, then
              paste it above.
            </li>
          </ol>
          <Button
            color="subtle"
            size="md"
            className="w-full"
            disabled={busy || authorizing || !resolvedUrl || !connectToken.trim()}
            icon={busy ? <SpinnerIcon className="animate-spin" size={16} /> : undefined}
            onClick={() => void handleManualConnect()}
          >
            {busy ? 'Connecting' : 'Connect with token'}
          </Button>
        </div>
      </Disclosure>
    </div>
  )
}
