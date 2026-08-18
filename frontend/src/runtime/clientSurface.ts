import { isTauri } from '@tauri-apps/api/core'

export type ClientSurface = 'browser' | 'desktop_app' | 'phone'

const STORAGE_KEY = 'jarvis_surface'
const SURFACE_PARAM = 'jarvis_surface'

/** Read `?jarvis_surface=phone` (or browser) from the URL and persist for reloads. */
export function initClientSurface(): void {
  const params = new URLSearchParams(window.location.search)
  const fromUrl = params.get(SURFACE_PARAM)
  if (fromUrl !== 'phone' && fromUrl !== 'browser') return
  sessionStorage.setItem(STORAGE_KEY, fromUrl)
  params.delete(SURFACE_PARAM)
  const nextSearch = params.toString()
  const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ''}${window.location.hash}`
  window.history.replaceState({}, '', nextUrl)
}

export function getClientSurface(): ClientSurface {
  if (isTauri()) return 'desktop_app'
  const stored = sessionStorage.getItem(STORAGE_KEY)
  if (stored === 'phone') return 'phone'
  return 'browser'
}

export function isDesktopApp(): boolean {
  return isTauri()
}

export function isPhoneCompanion(): boolean {
  return getClientSurface() === 'phone'
}

export function suggestedPhoneName(): string {
  if (/iPhone|iPod/i.test(navigator.userAgent)) return 'iPhone'
  if (/Android/i.test(navigator.userAgent)) return 'Android phone'
  return 'Phone'
}
