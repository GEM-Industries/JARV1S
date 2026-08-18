/** Humanize opaque node IDs for conversation surfaces. */
export function formatNodeLabel(value?: string | null): string {
  const raw = (value ?? '').trim()
  if (!raw) return 'This device'

  const lower = raw.toLowerCase()
  if (lower === 'user' || lower === 'jarv1s') return 'This device'

  const prefixed = raw.match(/^([a-zA-Z]+)[-_]([0-9a-f]{6,}|[0-9a-f-]{20,})$/i)
  if (prefixed) {
    const kind = prefixed[1].toLowerCase()
    if (kind === 'browser') return 'Browser'
    if (kind === 'desktop') return 'Desktop'
    if (kind === 'mobile') return 'Mobile'
    return kind.charAt(0).toUpperCase() + kind.slice(1)
  }

  if (/^[0-9a-f]{8}-[0-9a-f-]{20,}$/i.test(raw)) return 'Device'
  return raw
}

export function truncateTitle(value: string, max = 96): string {
  const cleaned = value.replace(/\s+/g, ' ').trim()
  if (cleaned.length <= max) return cleaned
  return `${cleaned.slice(0, max - 1).trimEnd()}…`
}

export function formatSessionWhen(startedAt: string, endedAt: string): string {
  const start = new Date(startedAt)
  const end = new Date(endedAt)
  if (Number.isNaN(start.getTime())) return startedAt

  const sameMinute = Math.abs(end.getTime() - start.getTime()) < 60_000
  const datePart = start.toLocaleDateString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
  })
  const timePart = start.toLocaleTimeString(undefined, {
    hour: 'numeric',
    minute: '2-digit',
  })

  if (sameMinute || Number.isNaN(end.getTime())) {
    return `${datePart} · ${timePart}`
  }

  const endTime = end.toLocaleTimeString(undefined, {
    hour: 'numeric',
    minute: '2-digit',
  })
  return `${datePart} · ${timePart} – ${endTime}`
}

export function formatModality(value?: string | null): string | null {
  if (!value) return null
  if (value === 'voice') return 'Voice'
  if (value === 'text') return 'Text'
  if (value === 'multimodal') return 'Multimodal'
  return value.charAt(0).toUpperCase() + value.slice(1)
}
