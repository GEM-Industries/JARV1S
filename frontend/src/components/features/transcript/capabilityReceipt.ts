const SUMMARY_KEYS = ['query', 'q', 'title', 'name', 'search', 'message', 'text', 'entity_id'] as const

const SECRET_KEYS = new Set([
  'password',
  'token',
  'access_token',
  'refresh_token',
  'api_key',
  'apikey',
  'authorization',
  'secret',
  'client_secret',
])

const FAILURE_PREFIX = /^(EXCEPTION during execution:|Error: Code execution timed out|Security Violation:)/i

export type ReceiptLink = {
  title: string
  url?: string
}

export type ReceiptView = {
  title: string
  subtitle?: string
  statusLabel: 'Running' | 'Failed' | null
  facts: Array<{ key: string; value: string }>
  links: ReceiptLink[] | null
  output?: string
  outputKind?: 'text' | 'json'
}

function formatName(value: string): string {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

function parsePreview(code?: string | null): { fqn: string; args: Record<string, unknown> } {
  const match = code?.trim().match(/^([A-Za-z_][\w.]*)\(([\s\S]*)$/)
  if (!match) return { fqn: '', args: {} }

  let raw = match[2].trim()
  if (raw.endsWith(')')) raw = raw.slice(0, -1).trim()
  if (raw.endsWith('…')) raw = raw.slice(0, -1).trim()

  let args: Record<string, unknown> = {}
  if (raw) {
    try {
      const parsed: unknown = JSON.parse(raw)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        args = parsed as Record<string, unknown>
      }
    } catch {
      // Truncated previews are title-only.
    }
  }
  return { fqn: match[1], args }
}

function visibleArgs(args: Record<string, unknown>): Record<string, unknown> {
  const visible: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(args)) {
    if (SECRET_KEYS.has(key.toLowerCase())) continue
    visible[key] = value
  }
  return visible
}

function formatValue(value: unknown): string {
  if (value == null) return ''
  if (typeof value === 'string') {
    if (/^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(value)) return formatName(value)
    return value
  }
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function parseResult(raw?: string | null): unknown {
  if (!raw) return undefined
  const cleaned = raw
    .replace(/^(Success|Result|Error|EXCEPTION during execution):\s*/i, '')
    .trim()
  if (!cleaned) return undefined
  try {
    return JSON.parse(cleaned) as unknown
  } catch {
    return cleaned
  }
}

function asLinks(value: unknown): ReceiptLink[] | null {
  if (!Array.isArray(value) || value.length === 0) return null
  const hits: ReceiptLink[] = []
  for (const item of value) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null
    const record = item as Record<string, unknown>
    if (typeof record.title !== 'string' || !record.title.trim()) return null
    hits.push({
      title: record.title.trim(),
      url: typeof record.url === 'string' ? safeHttpUrl(record.url) : undefined,
    })
  }
  return hits
}

export function safeHttpUrl(url?: string): string | undefined {
  if (!url) return undefined
  try {
    const parsed = new URL(url)
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return undefined
    return parsed.href
  } catch {
    return undefined
  }
}

export function hostnameOf(url?: string): string | undefined {
  const href = safeHttpUrl(url)
  if (!href) return undefined
  return new URL(href).hostname.replace(/^www\./, '')
}

export function buildReceipt(
  code?: string | null,
  result?: string | null,
  status?: 'running' | 'completed' | 'error',
): ReceiptView {
  const { fqn, args } = parsePreview(code)
  const visible = visibleArgs(args)
  const failed = FAILURE_PREFIX.test(result?.trim() || '')
  const running = status === 'running' && !result
  const parsed = parseResult(result)
  const links = asLinks(parsed)

  let subtitle: string | undefined
  for (const key of SUMMARY_KEYS) {
    const value = visible[key]
    if (typeof value === 'string' && value.trim()) {
      subtitle = value.trim()
      break
    }
  }
  if (!subtitle && running) subtitle = 'Working…'
  if (!subtitle && (failed || status === 'error')) {
    subtitle = typeof parsed === 'string' && parsed ? parsed : 'Failed'
  }
  if (!subtitle && Array.isArray(parsed) && parsed.length > 0) {
    subtitle = parsed.length === 1 ? '1 result' : `${parsed.length} results`
  }

  const facts = Object.entries(visible).map(([key, value]) => ({
    key: formatName(key),
    value: formatValue(value),
  }))

  let output: string | undefined
  let outputKind: 'text' | 'json' | undefined
  if (!links && parsed !== undefined) {
    if (typeof parsed === 'string') {
      output = parsed
      outputKind = 'text'
    } else {
      output = JSON.stringify(parsed, null, 2)
      outputKind = 'json'
    }
  }

  return {
    title: fqn ? fqn.split('.').filter(Boolean).map(formatName).join(' · ') : 'Tool',
    subtitle,
    statusLabel: running ? 'Running' : failed || status === 'error' ? 'Failed' : null,
    facts,
    links,
    output,
    outputKind,
  }
}
