import { TranscriptItem } from '../../../types'

export type TranscriptTurn =
  | { kind: 'user'; id: string; item: TranscriptItem }
  | { kind: 'assistant'; id: string; items: TranscriptItem[] }
  | { kind: 'notice'; id: string; item: TranscriptItem }

function isAssistantPart(item: TranscriptItem): boolean {
  return item.type === 'code' || item.type === 'reasoning' || (
    item.type === 'text' && item.sender === 'assistant'
  )
}

export function groupTranscriptTurns(items: TranscriptItem[]): TranscriptTurn[] {
  const turns: TranscriptTurn[] = []

  for (const item of items) {
    if (item.type === 'notice') {
      turns.push({ kind: 'notice', id: item.id, item })
      continue
    }

    if (item.type === 'text' && item.sender === 'user') {
      turns.push({ kind: 'user', id: item.id, item })
      continue
    }

    if (isAssistantPart(item)) {
      const previous = turns[turns.length - 1]
      if (previous?.kind === 'assistant') {
        previous.items.push(item)
      } else {
        turns.push({ kind: 'assistant', id: item.id, items: [item] })
      }
    }
  }

  return turns
}

export function turnTimestamp(turn: TranscriptTurn): number {
  if (turn.kind === 'assistant') return turn.items[0]?.timestamp ?? 0
  return turn.item.timestamp
}

export function formatTranscriptTime(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString([], {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  })
}
