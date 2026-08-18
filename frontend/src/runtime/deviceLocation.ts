import { isDesktopApp } from './clientSurface'
import { getDeviceLocation } from './desktopBridge'

export interface DeviceGpsPayload {
  latitude: number
  longitude: number
  source: 'gps'
  accuracy_m?: number
  captured_at: string
}

/** Result of a GPS resolve. `unavailableReason` is set only for desktop Host failures. */
export interface DeviceGpsResult {
  location: DeviceGpsPayload | null
  unavailableReason?: string
}

let inFlight: Promise<DeviceGpsResult> | null = null

/** Resolve ephemeral GPS for `context.update`. Coalesces concurrent callers. */
export function resolveDeviceGps(): Promise<DeviceGpsResult> {
  if (inFlight) return inFlight
  inFlight = resolveOnce().finally(() => {
    inFlight = null
  })
  return inFlight
}

async function resolveOnce(): Promise<DeviceGpsResult> {
  if (isDesktopApp()) {
    return resolveDesktopGps()
  }
  return { location: await resolveBrowserGps() }
}

async function resolveDesktopGps(): Promise<DeviceGpsResult> {
  try {
    const fix = await getDeviceLocation()
    return {
      location: {
        latitude: fix.latitude,
        longitude: fix.longitude,
        source: 'gps',
        accuracy_m:
          typeof fix.accuracy_m === 'number' && Number.isFinite(fix.accuracy_m)
            ? fix.accuracy_m
            : undefined,
        captured_at: fix.captured_at,
      },
    }
  } catch (error) {
    return { location: null, unavailableReason: invokeErrorReason(error) }
  }
}

function resolveBrowserGps(): Promise<DeviceGpsPayload | null> {
  return new Promise((resolve) => {
    if (!navigator.geolocation) {
      resolve(null)
      return
    }

    navigator.geolocation.getCurrentPosition(
      (position) => {
        resolve({
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          source: 'gps',
          accuracy_m:
            typeof position.coords.accuracy === 'number'
              ? position.coords.accuracy
              : undefined,
          captured_at: new Date(position.timestamp).toISOString(),
        })
      },
      () => {
        resolve(null)
      },
      { timeout: 5000, maximumAge: 60_000 },
    )
  })
}

function invokeErrorReason(error: unknown): string {
  if (typeof error === 'string' && error.trim()) {
    return error.trim().slice(0, 64)
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message.trim().slice(0, 64)
  }
  return 'unavailable'
}
