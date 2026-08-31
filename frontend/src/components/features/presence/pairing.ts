export function pairingExpiryLabel(
  expiresAt: string,
  now: number,
  expiredText = 'Code expired',
): string {
  const remainingSeconds = Math.max(0, Math.ceil((new Date(expiresAt).getTime() - now) / 1000))
  if (remainingSeconds === 0) return expiredText
  const minutes = Math.floor(remainingSeconds / 60)
  const seconds = String(remainingSeconds % 60).padStart(2, '0')
  return `Expires in ${minutes}:${seconds}`
}

export function satellitePairCommand(code: string, wsUrl?: string | null): string {
  if (wsUrl) return `jarvis-satellite pair ${code} --url ${wsUrl}`
  return `jarvis-satellite pair ${code}`
}

export const SPEAKER_CONNECT_WAIT_MS = 120_000

export const PAIRING_FALLBACK_HINT = 'On the speaker, paste:'

export type LanPairStatus = 'idle' | 'connecting' | 'ok' | 'failed' | 'skipped'

export function showSpeakerPairCommand(
  status: LanPairStatus,
  options: { connected: boolean; waiting: boolean },
): boolean {
  if (options.connected || status === 'connecting') return false
  if (status === 'ok' && options.waiting) return false
  return status !== 'idle'
}
