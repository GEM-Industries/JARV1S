import { isDesktopApp } from './clientSurface'

type InvokeFn = <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>

async function getInvoke(): Promise<InvokeFn | null> {
  if (!isDesktopApp()) return null
  try {
    const mod = await import('@tauri-apps/api/core')
    return mod.invoke as InvokeFn
  } catch {
    return null
  }
}

export interface HostReachabilityStatus {
  state: 'online' | 'degraded' | 'offline'
  backend_healthy: boolean
  remote_healthy?: boolean | null
  backend_url?: string | null
  tailscale: 'connected' | 'offline' | 'not_installed' | 'unknown'
  serve_url?: string | null
  funnel_url?: string | null
  funnel_configured?: boolean
  funnel_needs_consent?: boolean
  sleep_risk: boolean
  detail?: string | null
}

export interface HostLaunchState {
  phase: 'check_prerequisites' | 'prepare_dependencies' | 'start_services' | 'start_backend'
    | 'wait_for_health' | 'resolve_setup_state' | 'ready' | 'failed'
  state: 'checking' | 'running' | 'waiting' | 'needs_setup' | 'ready' | 'degraded' | 'failed'
  message: string
  detail?: string | null
  backend_url?: string | null
  backend_port?: number | null
}

export interface HostPrefs {
  launch_at_login: boolean
  hide_on_close: boolean
  external_triggers_enabled: boolean
  managed_local_llm_enabled: boolean
}

export interface DiagnosticsExport {
  path: string
}

export interface CallActivity {
  active: boolean
  app?: string | null
  supported: boolean
}

export interface EnableHostServeResult {
  ok: boolean
  needs_consent: boolean
  consent_url?: string | null
  serve_url?: string | null
  detail?: string | null
}

export interface EnableHostFunnelResult {
  ok: boolean
  needs_consent: boolean
  consent_url?: string | null
  funnel_url?: string | null
  detail?: string | null
}

export interface SpeakerReachability {
  network: 'online' | 'offline' | 'not_found' | 'unavailable'
  last_seen?: string | null
}

export interface PairSpeakerResult {
  ok: boolean
  node_id?: string | null
  detail?: string | null
}

export async function checkSpeakerReachability(
  nodeId: string,
): Promise<SpeakerReachability | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<SpeakerReachability>('check_speaker_reachability', { nodeId })
}

export async function pairSpeakerFromHost(args: {
  code: string
  backendUrl?: string | null
  nodeId?: string | null
}): Promise<PairSpeakerResult | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<PairSpeakerResult>('pair_speaker', {
    code: args.code,
    backendUrl: args.backendUrl ?? null,
    nodeId: args.nodeId ?? null,
  })
}

export async function getHostStatus(): Promise<HostReachabilityStatus | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<HostReachabilityStatus>('get_host_status')
}

export async function getHostLaunchState(): Promise<HostLaunchState | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<HostLaunchState>('get_launch_state')
}

export async function getCallActivity(): Promise<CallActivity | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<CallActivity>('get_call_activity')
}

export async function listenForCallActivity(
  handler: (activity: CallActivity) => void,
): Promise<() => void> {
  if (!isDesktopApp()) return () => {}
  const { listen } = await import('@tauri-apps/api/event')
  return listen<CallActivity>('call-activity-update', ({ payload }) => handler(payload))
}

/** Convert Tailscale Serve (or other HTTPS origin) into the satellite WebSocket URL. */
export function wsUrlFromHostOrigin(origin: string | null | undefined): string | null {
  if (!origin) return null
  const base = origin.replace(/\/$/, '')
  if (base.startsWith('https://')) return `${base.replace(/^https:/, 'wss:')}/api/v1/ws`
  if (base.startsWith('http://')) return `${base.replace(/^http:/, 'ws:')}/api/v1/ws`
  return null
}

export async function enableHostServe(): Promise<EnableHostServeResult | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<EnableHostServeResult>('enable_host_serve_cmd')
}

export async function enableHostFunnel(): Promise<EnableHostFunnelResult | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<EnableHostFunnelResult>('enable_host_funnel_cmd')
}

export async function disableHostFunnel(): Promise<EnableHostFunnelResult | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<EnableHostFunnelResult>('disable_host_funnel_cmd')
}

export async function getHostPrefs(): Promise<HostPrefs | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<HostPrefs>('get_host_prefs')
}

export interface DeviceLocationFix {
  latitude: number
  longitude: number
  accuracy_m?: number | null
  captured_at: string
}

/** One-shot Host Core Location fix. Rejects with a short reason string. */
export async function getDeviceLocation(): Promise<DeviceLocationFix> {
  const invoke = await getInvoke()
  if (!invoke) {
    throw new Error('unavailable')
  }
  return invoke<DeviceLocationFix>('get_device_location')
}

export async function setHostPrefs(prefs: HostPrefs): Promise<HostPrefs | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<HostPrefs>('set_host_prefs', { prefs })
}

/** Persist preference and start/stop the managed Ollama sidecar atomically. */
export async function setManagedLocalLlmEnabled(enabled: boolean): Promise<boolean | null> {
  const invoke = await getInvoke()
  if (!invoke) return null
  return invoke<boolean>('set_managed_local_llm_enabled_cmd', { enabled })
}

export async function restartHost(): Promise<void> {
  const invoke = await getInvoke()
  if (!invoke) return
  await invoke('restart_host')
}

export async function openExternalUrlViaHost(url: string): Promise<void> {
  const invoke = await getInvoke()
  if (!invoke) {
    throw new Error('Opening links is available only in the desktop app')
  }
  await invoke('open_external_url', { url })
}

export async function exportDiagnosticsBundle(
  includeUserContent = false,
  client?: object | null,
): Promise<DiagnosticsExport> {
  const invoke = await getInvoke()
  if (!invoke) {
    throw new Error('Diagnostics export is available only in the desktop app')
  }
  return invoke<DiagnosticsExport>('export_diagnostics_bundle', {
    request: {
      include_user_content: includeUserContent,
      client: client ?? undefined,
    },
  })
}
