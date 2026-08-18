import { setupApi, type ManagedLlmStatus } from './setupApi'
import { isDesktopApp } from '../runtime/clientSurface'
import { setManagedLocalLlmEnabled } from '../runtime/desktopBridge'

const POLL_INTERVAL_MS = 1500
const MAX_POLLS = 600 // ~15 minutes
const RUNTIME_READY_POLLS = 20
const RUNTIME_READY_INTERVAL_MS = 250

export type ManagedLocalPhase = 'checking' | 'starting' | 'downloading' | 'ready'

function formatInvokeError(cause: unknown, fallback: string): Error {
  if (cause instanceof Error && cause.message.trim()) return cause
  if (typeof cause === 'string' && cause.trim()) return new Error(cause)
  if (cause && typeof cause === 'object') {
    const record = cause as Record<string, unknown>
    for (const key of ['message', 'error', 'detail'] as const) {
      const value = record[key]
      if (typeof value === 'string' && value.trim()) return new Error(value)
    }
  }
  return new Error(fallback)
}

async function waitForRuntimeReady(
  onStatus: (status: ManagedLlmStatus) => void,
): Promise<ManagedLlmStatus> {
  let status = await setupApi.getManagedLocalStatus()
  onStatus(status)
  if (status.runtime_ready && status.status !== 'runtime_down') return status

  for (let i = 0; i < RUNTIME_READY_POLLS; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, RUNTIME_READY_INTERVAL_MS))
    status = await setupApi.getManagedLocalStatus()
    onStatus(status)
    if (status.runtime_ready && status.status !== 'runtime_down') return status
  }
  throw new Error(status.detail || 'Could not start the on-device model runtime.')
}

export class ManagedLocalDownloadPausedError extends Error {
  constructor(message = 'Download paused. Resume anytime — progress is kept.') {
    super(message)
    this.name = 'ManagedLocalDownloadPausedError'
  }
}

/**
 * Bring the JARV1S-managed local model to a ready state: start the sidecar
 * (desktop), install on demand, and poll download progress. `onStatus` fires on
 * every state change so callers can render their own UI. Throws a user-facing
 * Error on failure; resolves with the ready status.
 */
export async function ensureManagedLocalReady(
  onStatus: (status: ManagedLlmStatus) => void,
  onPhase?: (phase: ManagedLocalPhase) => void,
): Promise<ManagedLlmStatus> {
  onPhase?.('checking')
  let status = await setupApi.getManagedLocalStatus()
  onStatus(status)
  if (!status.supported) {
    throw new Error(status.detail || 'This Mac cannot run the on-device model.')
  }

  if (isDesktopApp()) {
    onPhase?.('starting')
    try {
      const started = await setManagedLocalLlmEnabled(true)
      if (started !== true) {
        throw new Error(
          'Could not start the on-device model runtime. Restart JARV1S and try again.',
        )
      }
    } catch (cause) {
      throw formatInvokeError(cause, 'Could not start the on-device model runtime.')
    }
    status = await waitForRuntimeReady(onStatus)
  } else {
    status = await setupApi.getManagedLocalStatus()
    onStatus(status)
  }

  if (status.status === 'ready') {
    onPhase?.('ready')
    return status
  }
  if (status.status === 'runtime_down' || !status.runtime_ready) {
    throw new Error(status.detail || 'Start JARV1S Host to use the on-device model.')
  }

  onPhase?.('downloading')
  status = await setupApi.installManagedLocal()
  onStatus(status)
  if (status.status === 'ready') {
    onPhase?.('ready')
    return status
  }
  if (status.status === 'failed' || status.status === 'unsupported' || status.status === 'runtime_down') {
    throw new Error(status.detail || 'Could not install the on-device model.')
  }
  if (status.status === 'absent') {
    throw new ManagedLocalDownloadPausedError(status.detail || undefined)
  }

  for (let i = 0; i < MAX_POLLS; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS))
    status = await setupApi.getManagedLocalStatus()
    onStatus(status)
    if (status.status === 'ready') {
      onPhase?.('ready')
      return status
    }
    if (status.status === 'failed') {
      throw new Error(status.detail || 'Model download failed.')
    }
    if (status.status === 'absent') {
      throw new ManagedLocalDownloadPausedError(status.detail || undefined)
    }
  }
  throw new Error('Model download is taking longer than expected. Keep JARV1S open and try again.')
}
