import React, { useCallback } from 'react'
import { CaretRightIcon } from '@phosphor-icons/react'
import { openBackgroundTaskWidget } from '../widgets/openBackgroundTaskWidget'
import { cn } from '../../../utils/cn'
import { StatusPill } from '../../ui/StatusPill'
import { OUTCOME_META } from './outcome'
import type { ActivityEntry } from '../../../types/operations'

export const formatRelativeWhen = (when: string): string => {
  const parsed = Date.parse(when)
  if (Number.isNaN(parsed)) return when
  const diffMs = Date.now() - parsed
  const mins = Math.floor(diffMs / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

const categoryLabel: Record<ActivityEntry['category'], string> = {
  conversation: 'Conversation',
  reminder: 'Reminder',
  automation: 'Automation',
  task: 'Task',
  system: 'System',
}

async function openTaskFromActivity(taskId: string, onClose: () => void): Promise<void> {
  const opened = await openBackgroundTaskWidget(taskId, { pinned: true })
  if (opened) onClose()
}

export const ActivityRow: React.FC<{
  item: ActivityEntry
  onClose?: () => void
  onSelect?: (item: ActivityEntry) => void
  selected?: boolean
}> = React.memo(({ item, onClose, onSelect, selected = false }) => {
  const isTask = item.detail_ref.kind === 'background_task'
  const canOpen = isTask || Boolean(onSelect)
  const outcome = OUTCOME_META[item.outcome]

  const handleOpen = useCallback(() => {
    if (isTask) {
      void openTaskFromActivity(item.task_id ?? item.detail_ref.id, onClose ?? (() => undefined))
      return
    }
    onSelect?.(item)
  }, [isTask, item, onClose, onSelect])

  return (
    <div className={cn(
      'relative overflow-hidden rounded-control border transition-colors duration-feedback',
      selected
        ? 'border-brand/35 bg-surface/20'
        : 'border-transparent bg-surface/[0.07] hover:bg-surface/15',
    )}>
      <button
        type="button"
        onClick={handleOpen}
        disabled={!canOpen}
        aria-current={selected ? 'true' : undefined}
        className={cn(
          'min-h-11 w-full px-3 py-3 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70 focus-visible:ring-inset',
          canOpen && 'cursor-pointer hover:bg-surface/20 transition-colors',
        )}
      >
        <div className="flex items-stretch gap-3">
          <div className="relative flex w-3 shrink-0 justify-center pt-1" aria-hidden>
            <span className={cn('absolute bottom-0 top-4 w-px bg-gradient-to-b to-transparent', outcome.rail)} />
            <span className={cn('relative h-2 w-2 rounded-full border', outcome.node)} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-start justify-between gap-3">
              <p className="min-w-0 flex-1 line-clamp-2 type-body text-foreground">
                {item.title}
              </p>
              <span className="shrink-0 type-meta tabular-nums text-foreground-subtle">
                {formatRelativeWhen(item.occurred_at)}
              </span>
            </div>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
              <span className="min-w-0 truncate type-meta text-foreground-subtle">
                {categoryLabel[item.category]}
                {item.delivery ? ` · ${item.delivery}` : ''}
                {item.source_label ? ` · ${item.source_label}` : ''}
              </span>
              {item.outcome === 'succeeded' && !item.failure_label ? (
                <span className="type-meta text-status-success/80">{outcome.label}</span>
              ) : (
                <StatusPill
                  tone={outcome.tone}
                  className={cn(
                    'max-w-full',
                    item.failure_label && 'normal-case tracking-[0.04em]',
                  )}
                  title={item.failure_label ?? outcome.label}
                >
                  {item.failure_label ?? outcome.label}
                </StatusPill>
              )}
            </div>
            {item.summary && (
              <p className="mt-2 line-clamp-2 type-meta text-foreground-muted">
                {item.summary}
              </p>
            )}
          </div>
          {canOpen && (
            <span className="mt-0.5 shrink-0 text-foreground-subtle">
              <CaretRightIcon size={14} />
            </span>
          )}
        </div>
      </button>
    </div>
  )
})
ActivityRow.displayName = 'ActivityRow'

