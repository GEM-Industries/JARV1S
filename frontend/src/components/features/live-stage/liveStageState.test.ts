import { describe, expect, it } from 'vitest'
import type { AgentState, TranscriptItem, UIEnvelope } from '../../../types'
import type { LiveAssistantPreview } from '../../../store/useJarvisStore'
import {
  deriveLiveStagePresentation,
  resolveLiveStagePhase,
} from './liveStageState'

const envelope = (
  partial: Partial<UIEnvelope> & Pick<UIEnvelope, 'widget_id' | 'component'>,
): UIEnvelope => ({
  created_at: 1,
  ...partial,
  data: partial.data ?? {},
  layout: partial.layout ?? { size: 'small', priority: 10 },
})

const textItem = (
  partial: Partial<TranscriptItem> & Pick<TranscriptItem, 'id' | 'sender' | 'text'>,
): TranscriptItem => ({
  type: 'text',
  timestamp: Date.now(),
  ...partial,
})

const base = {
  hostState: 'online' as const,
  connectionState: 'connected' as const,
  agentState: 'idle' as AgentState,
  isSpeaking: false,
  transcript: [] as TranscriptItem[],
  partialTranscript: null as string | null,
  liveAssistantPreview: null as LiveAssistantPreview | null,
  activeWidgetId: null as string | null,
  widgets: [] as UIEnvelope[],
}

describe('live stage presentation policy', () => {
  it('keeps Speaking authoritative while local playback outlives backend work state', () => {
    expect(resolveLiveStagePhase({
      hostState: 'online',
      connectionState: 'connected',
      agentState: 'running_tool',
      isSpeaking: true,
    })).toBe('speaking')
  })

  it('collapses composing_tool into Thinking', () => {
    expect(deriveLiveStagePresentation({
      ...base,
      agentState: 'composing_tool',
    }).phase).toBe('thinking')
  })

  it('shows live capture over an existing widget and surfaces the user partial', () => {
    const presentation = deriveLiveStagePresentation({
      ...base,
      agentState: 'listening',
      activeWidgetId: 'weather',
      widgets: [envelope({ widget_id: 'weather', component: 'WeatherWidget' })],
      partialTranscript: 'What is the weather in Sydney today',
    })
    expect(presentation.focalKind).toBe('projection')
    expect(presentation.userPreview).toContain('weather in Sydney')
  })

  it('promotes current-task consent into the centre and leaves background approval in the rail', () => {
    const presentation = deriveLiveStagePresentation({
      ...base,
      agentState: 'running_tool',
      activeWidgetId: 'weather',
      widgets: [
        envelope({ widget_id: 'weather', component: 'WeatherWidget' }),
        envelope({
          widget_id: 'approval',
          component: 'PendingInputWidget',
          data: { status: 'pending', prompt: 'Delete file?' },
          layout: { size: 'small', priority: 100 },
        }),
        envelope({
          widget_id: 'bg-approval',
          component: 'ContentWidget',
          data: { display: 'receipt', attention: 'approval', receipt_kind: 'task_progress' },
        }),
      ],
    })
    expect(presentation.focalKind).toBe('widget')
    expect(presentation.foregroundWidget?.widget_id).toBe('approval')
    expect(presentation.detail).toBe('Waiting for approval')
    expect(presentation.attentionReceiptIds).toEqual(['bg-approval'])
  })

  it('replaces speaking projection with a content widget', () => {
    const presentation = deriveLiveStagePresentation({
      ...base,
      agentState: 'speaking',
      isSpeaking: true,
      activeWidgetId: 'weather',
      widgets: [envelope({ widget_id: 'weather', component: 'WeatherWidget' })],
    })
    expect(presentation.focalKind).toBe('widget')
    expect(presentation.foregroundWidget?.widget_id).toBe('weather')
  })

  it('does not surface transcript history as a settled stage preview', () => {
    const presentation = deriveLiveStagePresentation({
      ...base,
      transcript: [
        textItem({ id: 'u1', sender: 'user', text: 'Tell me a story' }),
        textItem({ id: 'a1', sender: 'assistant', text: 'Once upon a' }),
      ],
    })
    expect(presentation.phase).toBe('idle')
    expect(presentation.assistantPreview).toBeNull()
    expect(presentation.settled).toBe(false)
  })

  it('settles a just-finished live reply from the ephemeral scratch buffer', () => {
    const presentation = deriveLiveStagePresentation({
      ...base,
      transcript: [
        textItem({ id: 'u1', sender: 'user', text: 'Tell me a story' }),
        textItem({ id: 'a1', sender: 'assistant', text: 'Once upon a' }),
      ],
      liveAssistantPreview: { text: 'Once upon a', key: 'a1' },
    })
    expect(presentation.phase).toBe('idle')
    expect(presentation.focalKind).toBe('projection')
    expect(presentation.userPreview).toBeNull()
    expect(presentation.assistantPreview).toBe('Once upon a')
    expect(presentation.assistantResponseKey).toBe('a1')
    expect(presentation.settled).toBe(true)
  })

  it('retains the live scratch during passive listening until speech-start clears it', () => {
    const presentation = deriveLiveStagePresentation({
      ...base,
      agentState: 'listening',
      transcript: [
        textItem({ id: 'u1', sender: 'user', text: 'Tell me a story' }),
        textItem({ id: 'a1', sender: 'assistant', text: 'Once upon a time' }),
      ],
      liveAssistantPreview: { text: 'Once upon a time', key: 'a1' },
    })

    expect(presentation.phase).toBe('listening')
    expect(presentation.assistantPreview).toBe('Once upon a time')
    expect(presentation.userPreview).toBeNull()
  })

  it('shows no assistant preview after scratch clear when listening without partials', () => {
    const presentation = deriveLiveStagePresentation({
      ...base,
      agentState: 'listening',
      transcript: [
        textItem({ id: 'u1', sender: 'user', text: 'Tell me a story' }),
        textItem({ id: 'a1', sender: 'assistant', text: 'Once upon a time' }),
      ],
      liveAssistantPreview: null,
    })

    expect(presentation.assistantPreview).toBeNull()
    expect(presentation.userPreview).toBeNull()
  })

  it('replaces the retained assistant response when user speech begins', () => {
    const presentation = deriveLiveStagePresentation({
      ...base,
      agentState: 'listening',
      partialTranscript: 'Wait, tell me another one',
      transcript: [
        textItem({ id: 'u1', sender: 'user', text: 'Tell me a story' }),
        textItem({ id: 'a1', sender: 'assistant', text: 'Once upon a time' }),
      ],
      // speech.start clears scratch; even if stale scratch remained, user preview wins
      liveAssistantPreview: { text: 'Once upon a time', key: 'a1' },
    })

    expect(presentation.assistantPreview).toBeNull()
    expect(presentation.assistantResponseKey).toBe('a1')
    expect(presentation.userPreview).toBe('Wait, tell me another one')
  })

  it('keeps the idle projection visible when conversation history exists', () => {
    const presentation = deriveLiveStagePresentation({
      ...base,
      transcript: [
        textItem({ id: 'u1', sender: 'user', text: 'Are you there?' }),
      ],
    })

    expect(presentation.phase).toBe('idle')
    expect(presentation.focalKind).toBe('projection')
    expect(presentation.settled).toBe(false)
  })
})
