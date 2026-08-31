import { describe, expect, it } from 'vitest'
import { describeBatch, presentTaskTrace, type TaskTraceItem } from './taskTrace'

const item = (kind: TaskTraceItem['kind'], extra: Partial<TaskTraceItem> = {}): TaskTraceItem => ({
  kind,
  ts: extra.ts ?? 1,
  ...extra,
})

describe('presentTaskTrace', () => {
  it('joins consecutive streamed text into one reply', () => {
    const entries = presentTaskTrace([
      item('text', { text_preview: 'The 10k Phase 2 quote is' }),
      item('text', { text_preview: 'live again and starts now.' }),
      item('tool_call', { tool: 'read: /tmp/a.md', ts: 2 }),
    ])
    expect(entries).toHaveLength(2)
    expect(entries[0]).toMatchObject({
      kind: 'reply',
      text: 'The 10k Phase 2 quote is live again and starts now.',
    })
  })

  it('collapses consecutive reads into one batch', () => {
    const entries = presentTaskTrace([
      item('tool_call', {
        tool: 'read: /repo/docs/plan.md',
        args_preview: { file_path: '/repo/docs/plan.md' },
        status: 'running',
      }),
      item('tool_result', {
        tool: 'read: /repo/docs/plan.md',
        args_preview: { file_path: '/repo/docs/plan.md' },
        status: 'completed',
        ts: 2,
      }),
      item('tool_call', {
        tool: 'read: /repo/docs/quote.md',
        args_preview: { file_path: '/repo/docs/quote.md' },
        ts: 3,
      }),
    ])
    expect(entries).toHaveLength(1)
    expect(entries[0]).toMatchObject({ kind: 'batch', action: 'Read' })
    if (entries[0].kind !== 'batch') throw new Error('expected batch')
    expect(entries[0].files.map((file) => file.name)).toEqual(['plan.md', 'quote.md'])
    expect(describeBatch(entries[0])).toBe('Read plan.md, quote.md')
  })

  it('summarizes a search that has no file path', () => {
    const entries = presentTaskTrace([
      item('tool_call', { tool: 'grep: context' }),
    ])
    expect(entries).toHaveLength(1)
    if (entries[0].kind !== 'batch') throw new Error('expected batch')
    expect(describeBatch(entries[0])).toBe('Search context')
  })
})
