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

function clusterTurnId(items: TranscriptItem[]): string | undefined {
  return items.find((item) => item.turn_id)?.turn_id
}

function continuesAssistantTurn(cluster: TranscriptItem[], next: TranscriptItem): boolean {
  const clusterId = clusterTurnId(cluster)
  return !(clusterId && next.turn_id && clusterId !== next.turn_id)
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
      if (previous?.kind === 'assistant' && continuesAssistantTurn(previous.items, item)) {
        previous.items.push(item)
      } else {
        turns.push({ kind: 'assistant', id: item.id, items: [item] })
      }
    }
  }

  return turns
}

export function isUnsolicitedTurn(
  previous: TranscriptTurn | undefined,
  turn: TranscriptTurn,
): boolean {
  return turn.kind === 'assistant' && previous?.kind !== 'user'
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
