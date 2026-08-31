import { describe, expect, it } from 'vitest'
import { TranscriptItem } from '../../../types'
import { groupTranscriptTurns, isUnsolicitedTurn, turnTimestamp } from './transcriptTurns'

function item(partial: Partial<TranscriptItem> & Pick<TranscriptItem, 'id' | 'type' | 'sender'>): TranscriptItem {
  return {
    timestamp: 1,
    ...partial,
  }
}

describe('groupTranscriptTurns', () => {
  it('keeps a receipt inside the following Jarvis outcome', () => {
    const turns = groupTranscriptTurns([
      item({ id: 'u1', type: 'text', sender: 'user', text: 'All right, turn them on.', timestamp: 10 }),
      item({ id: 'c1', type: 'code', sender: 'assistant', code: 'smart_home.control_lights({})', timestamp: 20 }),
      item({ id: 'a1', type: 'text', sender: 'assistant', text: 'Living room lights on.', timestamp: 30 }),
    ])

    expect(turns.map((turn) => turn.kind)).toEqual(['user', 'assistant'])
    expect(turns[1]).toMatchObject({
      kind: 'assistant',
      id: 'c1',
      items: [{ id: 'c1' }, { id: 'a1' }],
    })
    expect(turnTimestamp(turns[1])).toBe(20)
  })

  it('groups preamble, receipt, and outcome as one assistant turn', () => {
    const turns = groupTranscriptTurns([
      item({ id: 'u1', type: 'text', sender: 'user', text: 'search that' }),
      item({ id: 'a0', type: 'text', sender: 'assistant', text: 'On it.' }),
      item({ id: 'c1', type: 'code', sender: 'assistant', code: 'search.web({})' }),
      item({ id: 'a1', type: 'text', sender: 'assistant', text: 'Here is what I found.' }),
    ])

    expect(turns).toHaveLength(2)
    expect(turns[1]?.kind === 'assistant' && turns[1].items.map((part) => part.id)).toEqual([
      'a0',
      'c1',
      'a1',
    ])
  })

  it('does not merge consecutive user utterances or notices', () => {
    const turns = groupTranscriptTurns([
      item({ id: 'u1', type: 'text', sender: 'user', text: 'one' }),
      item({ id: 'u2', type: 'text', sender: 'user', text: 'two' }),
      item({ id: 'n1', type: 'notice', sender: 'system', text: "Jarvis didn't reply." }),
      item({ id: 'a1', type: 'text', sender: 'assistant', text: 'Later.' }),
    ])

    expect(turns.map((turn) => turn.kind)).toEqual(['user', 'user', 'notice', 'assistant'])
  })

  it('splits consecutive Jarvis turns that have different turn ids', () => {
    const turns = groupTranscriptTurns([
      item({ id: 'u1', type: 'text', sender: 'user', text: 'Turn the lights off.', timestamp: 10 }),
      item({
        id: 'c1',
        type: 'code',
        sender: 'assistant',
        turn_id: 'turn-lights',
        code: 'smart_home.control_lights({})',
        timestamp: 20,
      }),
      item({
        id: 'a1',
        type: 'text',
        sender: 'assistant',
        turn_id: 'turn-lights',
        text: 'Living room lights off.',
        timestamp: 21,
      }),
      item({
        id: 'a2',
        type: 'text',
        sender: 'assistant',
        turn_id: 'turn-wind-down',
        text: 'Time for wind down, sir.',
        timestamp: 22,
      }),
    ])

    expect(turns.map((turn) => turn.kind)).toEqual(['user', 'assistant', 'assistant'])
    expect(turns[1]?.kind === 'assistant' && turns[1].items.map((part) => part.id)).toEqual(['c1', 'a1'])
    expect(turns[2]?.kind === 'assistant' && turns[2].items.map((part) => part.id)).toEqual(['a2'])
    expect(isUnsolicitedTurn(turns[0], turns[1])).toBe(false)
    expect(isUnsolicitedTurn(turns[1], turns[2])).toBe(true)
  })

  it('still glues a live receipt that has no turn id onto the following outcome', () => {
    const turns = groupTranscriptTurns([
      item({ id: 'c1', type: 'code', sender: 'assistant', code: 'spotify.play({})' }),
      item({ id: 'a1', type: 'text', sender: 'assistant', turn_id: 'turn-play', text: 'Playing jazz.' }),
    ])

    expect(turns).toHaveLength(1)
    expect(turns[0]?.kind === 'assistant' && turns[0].items.map((part) => part.id)).toEqual(['c1', 'a1'])
  })

  it('keeps reasoning with the assistant turn', () => {
    const turns = groupTranscriptTurns([
      item({ id: 'r1', type: 'reasoning', sender: 'assistant', text: 'checking calendar' }),
      item({ id: 'c1', type: 'code', sender: 'assistant', code: 'calendar.get_events({})' }),
      item({ id: 'a1', type: 'text', sender: 'assistant', text: 'You are free.' }),
    ])

    expect(turns).toHaveLength(1)
    expect(turns[0]?.kind).toBe('assistant')
  })
})
