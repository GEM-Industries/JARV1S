import React from 'react'
import { CaretRightIcon } from '@phosphor-icons/react'
import { cn } from '../../../utils/cn'
import { StatusPill } from '../../ui/StatusPill'
import { formatRelativeWhen } from './ActivityTimeline'
import { formatNodeLabel, truncateTitle } from './conversationMeta'
import { OUTCOME_META } from './outcome'
import type { ActivityEntry, ActivityOutcome } from '../../../types/operations'

const SESSION_GAP_MS = 5 * 60_000

export interface ConversationSession {
  id: string
  title: string
  startedAt: string
  endedAt: string
  sourceLabel: string
  nodeId?: string | null
  outcome: ActivityOutcome
  turns: ActivityEntry[]
}

const outcomePriority: Record<ActivityOutcome, number> = {
  failed: 5,
  running: 4,
  waiting: 3,
  cancelled: 2,
  suppressed: 1,
  succeeded: 0,
}

function sessionOutcome(turns: ActivityEntry[]): ActivityOutcome {
  return turns.reduce<ActivityOutcome>(
    (current, turn) => (outcomePriority[turn.outcome] > outcomePriority[current]
      ? turn.outcome
      : current),
    'succeeded',
  )
}

function sourceKey(item: ActivityEntry): string {
  return item.node_id ?? item.source_key ?? item.source_label ?? 'user'
}

export function groupConversationSessions(
  items: ActivityEntry[],
  sessionGapMs = SESSION_GAP_MS,
): ConversationSession[] {
  const sessions: ConversationSession[] = []

  items
    .filter((item) => item.category === 'conversation')
    .forEach((item) => {
      const current = sessions[sessions.length - 1]
      const oldestCurrentTurn = current?.turns[current.turns.length - 1]
      const gapMs = oldestCurrentTurn
        ? Date.parse(oldestCurrentTurn.occurred_at) - Date.parse(item.occurred_at)
        : Number.POSITIVE_INFINITY
      const belongsToCurrent = current
        && sourceKey(current.turns[0]) === sourceKey(item)
        && gapMs >= 0
        && gapMs <= sessionGapMs

      if (belongsToCurrent) {
        current.turns.push(item)
        current.title = item.title
        current.startedAt = item.occurred_at
        current.outcome = sessionOutcome(current.turns)
        return
      }

      sessions.push({
        id: `conversation-session:${item.turn_id ?? item.activity_id}`,
        title: item.title,
        startedAt: item.occurred_at,
        endedAt: item.occurred_at,
        sourceLabel: formatNodeLabel(item.source_label ?? item.node_id),
        nodeId: item.node_id,
        outcome: item.outcome,
        turns: [item],
      })
    })

  return sessions
}

export const ConversationSessionRow: React.FC<{
  session: ConversationSession
  onSelect: (session: ConversationSession) => void
  selected?: boolean
}> = React.memo(({ session, onSelect, selected = false }) => {
  const exchangeLabel = `${session.turns.length} ${session.turns.length === 1 ? 'exchange' : 'exchanges'}`
  const needsAttention = session.outcome !== 'succeeded'
  const outcome = OUTCOME_META[session.outcome]

  return (
    <div className={cn(
      'relative overflow-hidden rounded-control border transition-colors duration-feedback',
      selected
        ? 'border-brand/35 bg-surface/20'
        : 'border-transparent bg-surface/[0.07] hover:bg-surface/15',
    )}>
      <button
        type="button"
        onClick={() => onSelect(session)}
        aria-current={selected ? 'true' : undefined}
        className="min-h-11 w-full cursor-pointer px-3 py-3 text-left transition-colors hover:bg-surface/20 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand/70"
      >
        <div className="flex items-stretch gap-3">
          <div className="relative flex w-3 shrink-0 justify-center pt-1" aria-hidden>
            <span className={cn('absolute bottom-0 top-4 w-px bg-gradient-to-b to-transparent', outcome.rail)} />
            <span className={cn('relative h-2 w-2 rounded-full border', outcome.node)} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-3">
              <p className="min-w-0 flex-1 line-clamp-2 type-body text-foreground">
                {truncateTitle(session.title, 120)}
              </p>
              <span className="shrink-0 type-meta tabular-nums text-foreground-subtle">
                {formatRelativeWhen(session.endedAt)}
              </span>
            </div>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
              <span className="type-meta text-foreground-subtle">
                Conversation · {session.sourceLabel}
              </span>
              <span className="type-meta text-foreground-subtle">{exchangeLabel}</span>
              {needsAttention ? (
                <StatusPill
                  tone={session.outcome === 'failed' ? 'error' : 'warning'}
                  className="normal-case tracking-[0.04em]"
                >
                  {session.outcome === 'failed' ? 'Needs attention' : outcome.label}
                </StatusPill>
              ) : (
                <span className="type-meta text-status-success/80">{outcome.label}</span>
              )}
            </div>
          </div>
          <span className="mt-0.5 shrink-0 text-foreground-subtle">
            <CaretRightIcon size={14} />
          </span>
        </div>
      </button>
    </div>
  )
})

ConversationSessionRow.displayName = 'ConversationSessionRow'
