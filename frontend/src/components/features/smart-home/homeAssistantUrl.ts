import { openExternalUrl } from '../../../utils/openExternalUrl'

export function normalizeHomeAssistantUrl(url: string): string | null {
  const trimmed = url.trim()
  if (!trimmed) return null

  try {
    const parsed = new URL(trimmed.includes('://') ? trimmed : `http://${trimmed}`)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null
    if (parsed.username || parsed.password) return null
    return parsed.origin
  } catch {
    return null
  }
}

export function homeAssistantHost(url?: string | null): string | null {
  if (!url) return null
  const normalized = normalizeHomeAssistantUrl(url)
  return normalized ? new URL(normalized).host : null
}

export function openHomeAssistant(url: string, path = '/'): void {
  const base = normalizeHomeAssistantUrl(url)
  if (base) void openExternalUrl(`${base}${path}`)
}
