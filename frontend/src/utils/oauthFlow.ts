import type { OAuthCallbackMessage } from '../types'
import { subscribeAuthOAuthChanged } from '../runtime/authEvents'
import { isDesktopApp } from '../runtime/clientSurface'
import { authorizedFetch } from '../client/http'

const DEFAULT_FEATURES = [
  'popup=yes',
  'width=520',
  'height=680',
  'resizable=yes',
  'scrollbars=yes',
].join(',')

const POPUP_TIMEOUT_MS = 5 * 60 * 1000
const EXTERNAL_TIMEOUT_MS = 2 * 60 * 1000

function escapeHtml(value: string) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function renderLoadingShell(popup: Window, title: string) {
  const safeTitle = escapeHtml(title)
  popup.document.write(`<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>${safeTitle}</title>
    <style>
      body {
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #0a0a0f;
        color: #e2e8f0;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      }
      .card {
        width: min(360px, calc(100vw - 48px));
        padding: 32px;
        border-radius: 18px;
        border: 1px solid rgba(125, 211, 252, 0.14);
        background: rgba(20, 20, 32, 0.94);
        text-align: center;
      }
      .spinner {
        width: 20px;
        height: 20px;
        margin: 0 auto 16px;
        border-radius: 999px;
        border: 2px solid rgba(125, 211, 252, 0.18);
        border-top-color: rgba(125, 211, 252, 0.92);
        animation: spin 0.8s linear infinite;
      }
      p {
        margin: 0;
        font-size: 14px;
        line-height: 1.6;
        color: #94a3b8;
      }
      @keyframes spin {
        to { transform: rotate(360deg); }
      }
    </style>
  </head>
  <body>
    <div class="card">
      <div class="spinner"></div>
      <p>Preparing authorization…</p>
    </div>
  </body>
</html>`)
  popup.document.close()
}

function openOAuthPopup(title: string): Window | null {
  const popup = window.open('', '_blank', DEFAULT_FEATURES)
  if (!popup) {
    return null
  }

  try {
    renderLoadingShell(popup, title)
  } catch {
    // Ignore cross-browser document access quirks.
  }

  popup.focus()
  return popup
}

function navigateOAuthPopup(popup: Window, url: string) {
  if (popup.closed) {
    throw new Error('The authorization window was closed before it could be used.')
  }
  popup.location.replace(url)
  popup.focus()
}

export function closeOAuthPopup(popup: Window | null | undefined) {
  if (popup && !popup.closed) {
    popup.close()
  }
}

async function openExternalAuthUrl(url: string): Promise<void> {
  const res = await authorizedFetch('/api/v1/auth/oauth/open-external', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      detail = json.detail ?? detail
    } catch {
      // ignore parse errors
    }
    throw new Error(detail)
  }
}

export type OAuthLaunchMode = 'popup' | 'external'

export interface BeginOAuthAuthorizationResult {
  mode: OAuthLaunchMode
  popup?: Window
}

export interface BeginOAuthAuthorizationOptions {
  /** Desktop/browser opener for non-allowlisted authorize URLs (e.g. local Home Assistant). */
  openExternal?: (url: string) => Promise<void>
}

export async function beginOAuthAuthorization(
  title: string,
  authUrl: string,
  options?: BeginOAuthAuthorizationOptions,
): Promise<BeginOAuthAuthorizationResult> {
  if (isDesktopApp()) {
    if (options?.openExternal) {
      await options.openExternal(authUrl)
    } else {
      await openExternalAuthUrl(authUrl)
    }
    return { mode: 'external' }
  }

  const popup = openOAuthPopup(title)
  if (popup) {
    navigateOAuthPopup(popup, authUrl)
    return { mode: 'popup', popup }
  }

  throw new Error('Allow pop-ups to authorize this integration.')
}

function toCallbackMessage(app: string, success: boolean, loaded?: boolean): OAuthCallbackMessage {
  return {
    type: 'jarvis:oauth_callback',
    success,
    app,
    loaded,
  }
}

interface WatchOAuthCompletionOptions {
  app: string
  mode: OAuthLaunchMode
  popup?: Window | null
  checkComplete?: () => Promise<boolean>
  onComplete: (message: OAuthCallbackMessage) => void
  onAborted: () => void
  timeoutMs?: number
}

export function watchOAuthCompletion({
  app,
  mode,
  popup,
  checkComplete,
  onComplete,
  onAborted,
  timeoutMs = mode === 'external' ? EXTERNAL_TIMEOUT_MS : POPUP_TIMEOUT_MS,
}: WatchOAuthCompletionOptions): () => void {
  let settled = false
  let popupWatchId: number | undefined
  let unsubscribeAuth: (() => void) | undefined

  const finish = (callback: () => void) => {
    if (settled) {
      return
    }
    settled = true
    if (popupWatchId !== undefined) {
      window.clearInterval(popupWatchId)
    }
    window.clearTimeout(timeoutId)
    window.removeEventListener('message', handlePostMessage)
    unsubscribeAuth?.()
    callback()
  }

  const accept = (message: OAuthCallbackMessage | { app: string; success: boolean; loaded?: boolean }) => {
    if (message.app !== app) {
      return
    }
    const payload = 'type' in message
      ? message
      : toCallbackMessage(message.app, message.success, message.loaded)
    finish(() => onComplete(payload))
  }

  const handlePostMessage = (event: MessageEvent) => {
    if (event.origin !== window.location.origin) {
      return
    }
    if (popup && event.source !== popup) {
      return
    }
    const message = event.data as OAuthCallbackMessage | undefined
    if (!message || message.type !== 'jarvis:oauth_callback') {
      return
    }
    accept(message)
  }

  if (mode === 'external') {
    unsubscribeAuth = subscribeAuthOAuthChanged((event) => {
      accept(event)
    })
  }

  if (mode === 'popup') {
    popupWatchId = window.setInterval(() => {
      if (popup && popup.closed) {
        finish(onAborted)
      }
    }, 500)
    window.addEventListener('message', handlePostMessage)
  }

  const timeoutId = window.setTimeout(() => {
    void (async () => {
      if (checkComplete) {
        try {
          if (await checkComplete()) {
            finish(() => onComplete(toCallbackMessage(app, true, true)))
            return
          }
        } catch {
          // fall through to abort
        }
      }
      finish(onAborted)
    })()
  }, timeoutMs)

  return () => {
    settled = true
    if (popupWatchId !== undefined) {
      window.clearInterval(popupWatchId)
    }
    window.clearTimeout(timeoutId)
    window.removeEventListener('message', handlePostMessage)
    unsubscribeAuth?.()
  }
}
