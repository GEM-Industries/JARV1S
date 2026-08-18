import { beforeEach, describe, expect, it, vi } from 'vitest'

const isTauriMock = vi.hoisted(() => vi.fn(() => false))

vi.mock('@tauri-apps/api/core', () => ({
  isTauri: isTauriMock,
}))

import {
  getClientSurface,
  initClientSurface,
  isDesktopApp,
  isPhoneCompanion,
} from './clientSurface'

function memoryStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
    removeItem: (key: string) => {
      values.delete(key)
    },
    clear: () => {
      values.clear()
    },
  }
}

function installBrowserGlobals(pathWithSearch: string) {
  const url = new URL(pathWithSearch, 'http://localhost')
  const storage = memoryStorage()
  const location = {
    pathname: url.pathname,
    search: url.search,
    hash: url.hash,
  }
  const history = {
    replaceState: (_state: unknown, _title: string, nextUrl: string) => {
      const next = new URL(nextUrl, 'http://localhost')
      location.pathname = next.pathname
      location.search = next.search
      location.hash = next.hash
    },
  }
  vi.stubGlobal('sessionStorage', storage)
  vi.stubGlobal('window', { location, history })
  vi.stubGlobal('navigator', {
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
  })
  return { location }
}

describe('clientSurface', () => {
  beforeEach(() => {
    isTauriMock.mockReturnValue(false)
    vi.unstubAllGlobals()
  })

  it('persists explicit phone mode from the URL and strips the param', () => {
    const { location } = installBrowserGlobals('/app?jarvis_surface=phone&keep=1')
    initClientSurface()
    expect(sessionStorage.getItem('jarvis_surface')).toBe('phone')
    expect(location.search).toBe('?keep=1')
    expect(isPhoneCompanion()).toBe(true)
    expect(getClientSurface()).toBe('phone')
  })

  it('defaults ordinary visits to browser even with a mobile UA', () => {
    installBrowserGlobals('/app')
    vi.stubGlobal('navigator', {
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
    })
    expect(getClientSurface()).toBe('browser')
    expect(isPhoneCompanion()).toBe(false)
  })

  it('reports desktop_app only when running inside Tauri', () => {
    installBrowserGlobals('/app')
    isTauriMock.mockReturnValue(true)
    expect(isDesktopApp()).toBe(true)
    expect(getClientSurface()).toBe('desktop_app')
  })

  it('ignores unknown surface query values', () => {
    const { location } = installBrowserGlobals('/app?jarvis_surface=desktop_app')
    initClientSurface()
    expect(sessionStorage.getItem('jarvis_surface')).toBeNull()
    expect(location.search).toBe('?jarvis_surface=desktop_app')
    expect(getClientSurface()).toBe('browser')
  })
})
