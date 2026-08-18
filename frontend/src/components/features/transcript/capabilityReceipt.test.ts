import { describe, expect, it } from 'vitest'
import { buildReceipt } from './capabilityReceipt'

describe('buildReceipt', () => {
  it('titles native calls from the preview string', () => {
    const receipt = buildReceipt('search.web({"query":"injector seal"})')
    expect(receipt.title).toBe('Search · Web')
    expect(receipt.subtitle).toBe('injector seal')
    expect(receipt.statusLabel).toBeNull()
  })

  it('does not dump raw JSON in the collapsed subtitle', () => {
    const receipt = buildReceipt(
      'search.web({"query":"injector seal"})',
      '[{"title":"Causes of Fuel Injector Leaks","url":"https://febest.eu/leaks"}]',
      'completed',
    )
    expect(receipt.subtitle).toBe('injector seal')
    expect(receipt.links).toEqual([
      { title: 'Causes of Fuel Injector Leaks', url: 'https://febest.eu/leaks' },
    ])
    expect(receipt.output).toBeUndefined()
  })

  it('still titles truncated previews without extra wire fields', () => {
    const receipt = buildReceipt('search.web({"query":"a very long search that gets cut…')
    expect(receipt.title).toBe('Search · Web')
  })

  it('redacts secrets and ignores javascript urls', () => {
    const receipt = buildReceipt(
      'search.web({"query":"ok","token":"secret"})',
      '[{"title":"Other","url":"javascript:alert(1)"}]',
    )
    expect(receipt.facts).toEqual([{ key: 'Query', value: 'ok' }])
    expect(receipt.links).toEqual([{ title: 'Other', url: undefined }])
  })

  it('shows running or failed as text status only', () => {
    expect(buildReceipt('todo.add_task({"title":"x"})', undefined, 'running').statusLabel).toBe('Running')
    expect(buildReceipt('todo.add_task({"title":"x"})', 'EXCEPTION during execution: boom', 'completed').statusLabel).toBe('Failed')
    expect(buildReceipt('todo.add_task({"title":"x"})', 'Added task.', 'completed').statusLabel).toBeNull()
  })

  it('keeps human results as text and objects as json', () => {
    expect(buildReceipt('smart_home.control_lights({"query":"x"})', 'Edison Bulb on.', 'completed').outputKind).toBe('text')
    expect(buildReceipt('todo.list({})', '{"items":[]}', 'completed').outputKind).toBe('json')
  })
})
