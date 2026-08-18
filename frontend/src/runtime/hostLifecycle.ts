import { useJarvisStore, type HostState } from '../store/useJarvisStore'
import { getHostLaunchState, type HostLaunchState } from './desktopBridge'
import { isDesktopApp } from './clientSurface'

type UnlistenFn = () => void

function hostStateFromLaunch(state: HostLaunchState): HostState {
  if (state.state === 'failed') return 'offline'
  if (state.state === 'degraded') return 'degraded'
  if (state.state === 'ready' || state.state === 'needs_setup') return 'online'
  return 'degraded'
}

export async function refreshHostState(): Promise<void> {
  const store = useJarvisStore.getState()
  if (isDesktopApp()) {
    try {
      const state = await getHostLaunchState()
      store.setHostState(state ? hostStateFromLaunch(state) : 'unknown')
    } catch {
      store.setHostState('offline')
    }
    return
  }

  try {
    const response = await fetch('/api/v1/health', { cache: 'no-store' })
    store.setHostState(response.ok ? 'online' : 'degraded')
  } catch {
    store.setHostState('offline')
  }
}

export async function startHostStateSync(): Promise<UnlistenFn> {
  await refreshHostState()
  if (!isDesktopApp()) return () => {}

  try {
    const { listen } = await import('@tauri-apps/api/event')
    return await listen<HostLaunchState>('host-launch-update', ({ payload }) => {
      useJarvisStore.getState().setHostState(hostStateFromLaunch(payload))
    })
  } catch {
    return () => {}
  }
}
