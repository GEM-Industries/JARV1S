import { beforeEach, describe, expect, it } from 'vitest'
import { useJarvisStore } from './useJarvisStore'

describe('transcript retract helpers', () => {
  beforeEach(() => {
    useJarvisStore.getState().clearTranscript()
  })

  it('removeTranscriptById drops only the provisional barge candidate row', () => {
    const store = useJarvisStore.getState()
    store.setUserTranscriptPreview({
      messageId: 'turn-candidate',
      text: 'Wow',
    })
    store.commitUserTranscript({
      id: 'turn-accepted',
      text: 'Keep me',
    })
    store.updateOrAddTranscriptItem({
      id: 'assistant-1',
      response_id: 'assistant-1',
      turn_id: 'turn-ai',
      text: 'Still speaking',
      sender: 'assistant',
      type: 'text',
      timestamp: Date.now(),
    })

    useJarvisStore.getState().removeTranscriptById('turn-candidate')

    const ids = useJarvisStore.getState().transcript.map((item) => item.id)
    expect(ids).toEqual(['turn-accepted', 'assistant-1'])
  })

  it('removeTranscriptByTurnId still only removes assistant rows', () => {
    const store = useJarvisStore.getState()
    store.commitUserTranscript({
      id: 'turn-shared',
      text: 'User keep',
    })
    store.updateOrAddTranscriptItem({
      id: 'assistant-1',
      response_id: 'assistant-1',
      turn_id: 'turn-shared',
      text: 'Assistant drop',
      sender: 'assistant',
      type: 'text',
      timestamp: Date.now(),
    })

    useJarvisStore.getState().removeTranscriptByTurnId('turn-shared')

    const remaining = useJarvisStore.getState().transcript
    expect(remaining).toHaveLength(1)
    expect(remaining[0]?.id).toBe('turn-shared')
    expect(remaining[0]?.sender).toBe('user')
  })
})
