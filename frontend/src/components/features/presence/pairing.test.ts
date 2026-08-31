import { describe, expect, it } from 'vitest'
import { satellitePairCommand, showSpeakerPairCommand } from './pairing'

describe('satellite pairing helpers', () => {
  it('includes the host url on first setup', () => {
    expect(
      satellitePairCommand('SHR-8YT', 'wss://macbook-pro.tail131191.ts.net:8443/api/v1/ws'),
    ).toBe(
      'jarvis-satellite pair SHR-8YT --url wss://macbook-pro.tail131191.ts.net:8443/api/v1/ws',
    )
  })

  it('hides the fallback command while this Mac is connecting', () => {
    expect(
      showSpeakerPairCommand('connecting', { connected: false, waiting: true }),
    ).toBe(false)
    expect(showSpeakerPairCommand('ok', { connected: false, waiting: true })).toBe(false)
    expect(showSpeakerPairCommand('ok', { connected: true, waiting: false })).toBe(false)
  })

  it('shows the speaker command when LAN pair fails or times out', () => {
    expect(
      showSpeakerPairCommand('failed', { connected: false, waiting: false }),
    ).toBe(true)
    expect(
      showSpeakerPairCommand('failed', { connected: false, waiting: true }),
    ).toBe(true)
    expect(showSpeakerPairCommand('ok', { connected: false, waiting: false })).toBe(true)
    expect(
      showSpeakerPairCommand('skipped', { connected: false, waiting: true }),
    ).toBe(true)
  })
})
