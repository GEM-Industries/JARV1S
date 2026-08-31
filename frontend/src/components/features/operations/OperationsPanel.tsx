import React, {
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from 'react'
import {
  ArrowCounterClockwiseIcon,
  ArrowLeftIcon,
  CaretDownIcon,
  FunnelIcon,
  PauseIcon,
  PlayIcon,
  TrashIcon,
} from '@phosphor-icons/react'
import {
  operationsApi,
  type ManagedSetup,
  type SetupKind,
  type SetupStatus,
} from '../../../client/operationsApi'
import {
  ingressApi,
  type InboundEventStats,
  type InboundEventSummary,
} from '../../../client/ingressApi'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { ActivityRow, formatRelativeWhen } from './ActivityTimeline'
import type {
  ActivityCategory,
  ActivityEntry,
  ActivityOutcome,
} from '../../../types/operations'
import { Button } from '../../ui/Button'
import { Chip } from '../../ui/Chip'
import { Placeholder } from '../../ui/Placeholder'
import { StatusPill } from '../../ui/StatusPill'
import { Select } from '../../ui/Select'
import { FieldControl, Input, SearchField } from '../../ui/FieldControl'
import { SegmentedTabs } from '../../ui/SegmentedTabs'
import { DataField, PanelSection } from '../../ui/PanelSection'
import { EmptyState } from '../../ui/EmptyState'
import { StatusBarWorkspaceHeader } from '../../ui/StatusBarWorkspaceHeader'
import { OUTCOME_META } from './outcome'
import { cn } from '../../../utils/cn'
import {
  ConversationSessionRow,
  groupConversationSessions,
  type ConversationSession,
} from './ConversationTimeline'

const OperationRunDetail = lazy(() => import('./OperationRunDetail').then((module) => ({
  default: module.OperationRunDetail,
})))
const ConversationSessionDetail = lazy(() => import('./ConversationSessionDetail').then((module) => ({
  default: module.ConversationSessionDetail,
})))

type Tab = 'activity' | 'configured'
type LoadState = 'idle' | 'loading' | 'ready' | 'error'
type TimeFilter = 'any' | 'day' | 'week' | 'month'

const categories: { key: ActivityCategory | 'all'; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'reminder', label: 'Reminders' },
  { key: 'automation', label: 'Automations' },
  { key: 'system', label: 'System' },
  { key: 'conversation', label: 'Conversations' },
]

const setupKinds: { key: SetupKind | 'all'; label: string }[] = [
  { key: 'all', label: 'All kinds' },
  { key: 'automation', label: 'Automations' },
  { key: 'schedule', label: 'Schedules' },
  { key: 'reminder', label: 'Reminders' },
  { key: 'timer', label: 'Timers' },
  { key: 'alarm', label: 'Alarms' },
  { key: 'deferred_instruction', label: 'Instructions' },
  { key: 'protocol', label: 'Protocols' },
]

const outcomeOptions = [
  { value: 'all', label: 'All' },
  ...Object.entries(OUTCOME_META).map(([value, meta]) => ({ value, label: meta.label })),
]

const timeOptions = [
  { value: 'any', label: 'Any time' },
  { value: 'day', label: 'Last 24h' },
  { value: 'week', label: 'Last 7d' },
  { value: 'month', label: 'Last 30d' },
]

const conversationStatusFilters: { value: 'all' | 'failed'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'failed', label: 'Needs attention' },
]

const conversationTimeFilters: { value: TimeFilter; label: string }[] = [
  { value: 'any', label: 'Any time' },
  { value: 'day', label: '24h' },
  { value: 'week', label: '7d' },
  { value: 'month', label: '30d' },
]

const setupStatusOptions = [
  { value: 'all', label: 'All statuses' },
  { value: 'active', label: 'Active' },
  { value: 'paused', label: 'Paused' },
  { value: 'disabled', label: 'Disabled' },
]

function sinceFor(filter: TimeFilter): string | undefined {
  if (filter === 'any') return undefined
  const days = filter === 'day' ? 1 : filter === 'week' ? 7 : 30
  return new Date(Date.now() - days * 86_400_000).toISOString()
}

function dayLabel(value: string): string {
  const date = new Date(value)
  const today = new Date()
  const yesterday = new Date(today)
  yesterday.setDate(today.getDate() - 1)
  if (date.toDateString() === today.toDateString()) return 'Today'
  if (date.toDateString() === yesterday.toDateString()) return 'Yesterday'
  return date.toLocaleDateString(undefined, {
    weekday: 'long',
    month: 'short',
    day: 'numeric',
  })
}

function formatDateTime(value?: string | null): string {
  if (!value) return 'Never'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function activityCategoryForSetup(kind: SetupKind): ActivityCategory {
  if (kind === 'automation') return 'automation'
  if (kind === 'protocol' || kind === 'deferred_instruction') return 'system'
  return 'reminder'
}

function displaySetupName(value: string): string {
  const normalized = value.replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim()
  return normalized ? normalized.charAt(0).toUpperCase() + normalized.slice(1) : value
}

export const OperationsPanelContent: React.FC = () => {
  const closeOverlay = useJarvisStore((state) => state.closeOverlay)
  const operationsRunsFilter = useJarvisStore((state) => state.operationsRunsFilter)
  const operationsVersion = useJarvisStore((state) => state.operationsVersion)
  const runsVersion = useJarvisStore((state) => state.runsVersion)

  const [tab, setTab] = useState<Tab>('activity')
  const [category, setCategory] = useState<ActivityCategory | 'all'>('all')
  const [outcome, setOutcome] = useState<ActivityOutcome | 'all'>('all')
  const [timeFilter, setTimeFilter] = useState<TimeFilter>('any')
  const [source, setSource] = useState('')
  const [search, setSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [nodeId, setNodeId] = useState<string | null>(null)
  const [filtersExpanded, setFiltersExpanded] = useState(false)
  const [desktopInspection, setDesktopInspection] = useState(false)
  const [activity, setActivity] = useState<ActivityEntry[]>([])
  const [activityState, setActivityState] = useState<LoadState>('idle')
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [selected, setSelected] = useState<ActivityEntry | null>(null)
  const [selectedConversation, setSelectedConversation] = useState<ConversationSession | null>(null)

  const [setupKind, setSetupKind] = useState<SetupKind | 'all'>('all')
  const [setupStatus, setSetupStatus] = useState<SetupStatus | 'all'>('all')
  const [setupSearch, setSetupSearch] = useState('')
  const [appliedSetupSearch, setAppliedSetupSearch] = useState('')
  const [setups, setSetups] = useState<ManagedSetup[]>([])
  const [setupState, setSetupState] = useState<LoadState>('idle')
  const [busySetups, setBusySetups] = useState<Set<string>>(new Set())
  const [setupErrors, setSetupErrors] = useState<Record<string, string>>({})
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null)
  const [expandedSetups, setExpandedSetups] = useState<Set<string>>(new Set())
  const [inboundStats, setInboundStats] = useState<InboundEventStats | null>(null)
  const [deadLetters, setDeadLetters] = useState<InboundEventSummary[]>([])
  const [retryingEventId, setRetryingEventId] = useState<string | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => setAppliedSearch(search.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    const timer = window.setTimeout(() => setAppliedSetupSearch(setupSearch.trim()), 250)
    return () => window.clearTimeout(timer)
  }, [setupSearch])

  useEffect(() => {
    const query = window.matchMedia('(min-width: 1024px)')
    const update = () => setDesktopInspection(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  useEffect(() => {
    if (!operationsRunsFilter) return
    const mapped: Partial<Record<typeof operationsRunsFilter.runKind, ActivityCategory | 'all'>> = {
      all: 'all',
      user: 'conversation',
      trigger: 'reminder',
      automation: 'automation',
      task: 'all',
      headless: 'system',
    }
    setTab('activity')
    setCategory(mapped[operationsRunsFilter.runKind] ?? 'all')
    setNodeId(operationsRunsFilter.nodeId ?? null)
  }, [operationsRunsFilter])

  const activityParams = useMemo(() => ({
    category: category === 'all' ? undefined : category,
    outcome: outcome === 'all' ? undefined : outcome,
    source: source.trim() || undefined,
    node_id: nodeId || undefined,
    since: sinceFor(timeFilter),
    search: appliedSearch || undefined,
  }), [appliedSearch, category, nodeId, outcome, source, timeFilter])

  const loadActivity = useCallback(async (cursor?: string) => {
    if (cursor) setLoadingMore(true)
    else setActivityState('loading')
    try {
      const page = await operationsApi.activityPage({
        ...activityParams,
        limit: 50,
        cursor,
      })
      setActivity((current) => cursor ? [...current, ...page.items] : page.items)
      setNextCursor(page.next_cursor ?? null)
      setHasMore(page.has_more)
      setActivityState('ready')
    } catch {
      if (!cursor) {
        setActivity([])
        setActivityState('error')
      }
    } finally {
      setLoadingMore(false)
    }
  }, [activityParams])

  useEffect(() => {
    if (tab !== 'activity') return
    setSelected(null)
    setSelectedConversation(null)
    void loadActivity()
  }, [loadActivity, runsVersion, tab])

  useEffect(() => {
    if (tab !== 'configured') return
    let cancelled = false
    setSetupState('loading')
    operationsApi.setups({
      kind: setupKind === 'all' ? undefined : setupKind,
      status: setupStatus === 'all' ? undefined : setupStatus,
      search: appliedSetupSearch || undefined,
    }).then((items) => {
      if (!cancelled) {
        setSetups(items)
        setSetupState('ready')
      }
    }).catch(() => {
      if (!cancelled) {
        setSetups([])
        setSetupState('error')
      }
    })
    return () => {
      cancelled = true
    }
  }, [
    appliedSetupSearch,
    operationsVersion.automations,
    operationsVersion.protocols,
    operationsVersion.schedules,
    setupKind,
    setupStatus,
    tab,
  ])

  useEffect(() => {
    if (tab !== 'configured') return
    let cancelled = false
    Promise.all([
      ingressApi.stats().catch(() => null),
      ingressApi.deadLetters(10).catch(() => [] as InboundEventSummary[]),
    ]).then(([stats, letters]) => {
      if (cancelled) return
      setInboundStats(stats)
      setDeadLetters(letters)
    })
    return () => {
      cancelled = true
    }
  }, [tab, runsVersion])

  const retryDeadLetter = async (eventId: string) => {
    setRetryingEventId(eventId)
    try {
      await ingressApi.retry(eventId)
      const [stats, letters] = await Promise.all([
        ingressApi.stats().catch(() => null),
        ingressApi.deadLetters(10).catch(() => [] as InboundEventSummary[]),
      ])
      setInboundStats(stats)
      setDeadLetters(letters)
    } catch {
      // Keep list as-is; user can retry again.
    } finally {
      setRetryingEventId(null)
    }
  }

  const groupedActivity = useMemo(() => {
    const groups: { label: string; items: ActivityEntry[] }[] = []
    activity.forEach((item) => {
      const label = dayLabel(item.occurred_at)
      const existing = groups[groups.length - 1]
      if (existing?.label === label) existing.items.push(item)
      else groups.push({ label, items: [item] })
    })
    return groups
  }, [activity])

  const groupedConversations = useMemo(() => {
    const groups: { label: string; items: ConversationSession[] }[] = []
    groupConversationSessions(activity).forEach((session) => {
      const label = dayLabel(session.endedAt)
      const existing = groups[groups.length - 1]
      if (existing?.label === label) existing.items.push(session)
      else groups.push({ label, items: [session] })
    })
    return groups
  }, [activity])

  useEffect(() => {
    if (!desktopInspection || activityState !== 'ready' || activity.length === 0) return
    if (category === 'conversation') {
      setSelected(null)
      setSelectedConversation((current) => (
        current && groupedConversations.some((group) => group.items.some((item) => item.id === current.id))
          ? current
          : groupedConversations[0]?.items[0] ?? null
      ))
      return
    }
    setSelectedConversation(null)
    setSelected((current) => (
      current && activity.some((item) => item.activity_id === current.activity_id)
        ? current
        : activity[0] ?? null
    ))
  }, [activity, activityState, category, desktopInspection, groupedConversations])

  const patchSetup = useCallback(async (item: ManagedSetup, patch: { enabled?: boolean; paused_until?: string | null }) => {
    const snapshot = item
    const optimistic: ManagedSetup = {
      ...item,
      paused_until: patch.paused_until === undefined
        ? item.paused_until
        : patch.paused_until,
      status: patch.enabled === false || patch.paused_until
        ? 'paused'
        : patch.enabled === true || patch.paused_until === null
          ? 'active'
          : item.status,
    }
    setBusySetups((current) => new Set(current).add(item.resource_ref))
    setSetupErrors((current) => {
      const next = { ...current }
      delete next[item.resource_ref]
      return next
    })
    setSetups((current) => current.map((entry) => entry.resource_ref === item.resource_ref ? optimistic : entry))
    try {
      const updated = await operationsApi.patchSetup(item.resource_ref, patch)
      setSetups((current) => current.map((entry) => entry.resource_ref === item.resource_ref ? updated : entry))
    } catch (error) {
      setSetups((current) => current.map((entry) => entry.resource_ref === item.resource_ref ? snapshot : entry))
      setSetupErrors((current) => ({
        ...current,
        [item.resource_ref]: error instanceof Error ? error.message : 'Update failed',
      }))
    } finally {
      setBusySetups((current) => {
        const next = new Set(current)
        next.delete(item.resource_ref)
        return next
      })
    }
  }, [])

  const deleteSetup = useCallback(async (item: ManagedSetup) => {
    setBusySetups((current) => new Set(current).add(item.resource_ref))
    try {
      await operationsApi.deleteSetup(item.resource_ref)
      setSetups((current) => current.filter((entry) => entry.resource_ref !== item.resource_ref))
      setConfirmingDelete(null)
    } catch (error) {
      setSetupErrors((current) => ({
        ...current,
        [item.resource_ref]: error instanceof Error ? error.message : 'Delete failed',
      }))
    } finally {
      setBusySetups((current) => {
        const next = new Set(current)
        next.delete(item.resource_ref)
        return next
      })
    }
  }, [])

  const viewSetupActivity = useCallback((item: ManagedSetup) => {
    setCategory(activityCategoryForSetup(item.kind))
    setSearch(item.name)
    setSource('')
    setOutcome('all')
    setTimeFilter('any')
    setTab('activity')
  }, [])

  const clearActivityFilters = useCallback(() => {
    setCategory('all')
    setOutcome('all')
    setTimeFilter('any')
    setSource('')
    setSearch('')
    setNodeId(null)
  }, [])

  const hasActivityFilters = category !== 'all'
    || outcome !== 'all'
    || timeFilter !== 'any'
    || Boolean(source || search || nodeId)
  const secondaryFilterCount = Number(outcome !== 'all')
    + Number(timeFilter !== 'any')
    + Number(Boolean(source || nodeId))
  const hasActivityResults = activityState === 'ready' && activity.length > 0

  const renderDetail = () => {
    if (category === 'conversation') {
      if (!selectedConversation) {
        return (
          <EmptyState
            className="m-4"
            title="Select a conversation"
            description="Choose a conversation to read its transcript."
          />
        )
      }
      return (
        <Suspense fallback={<Placeholder className="m-4">Loading conversation…</Placeholder>}>
          <ConversationSessionDetail
            session={selectedConversation}
            onBack={() => setSelectedConversation(null)}
          />
        </Suspense>
      )
    }
    if (!selected) {
      return (
        <EmptyState
          className="m-4"
          title="Select an activity"
          description="Choose an item from the timeline to inspect its source, delivery, and execution detail."
        />
      )
    }
    const outcomeMeta = OUTCOME_META[selected.outcome]
    return (
      <div className="p-6">
        <Button
          variant="ghost"
          color="action"
          size="sm"
          className="mb-3 h-10 lg:hidden"
          onClick={() => setSelected(null)}
          icon={<ArrowLeftIcon size={14} />}
        >
          Back to activity
        </Button>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[12px] font-mono uppercase tracking-[0.16em] text-foreground-subtle">
              {selected.category}
            </div>
            <h3 className="mt-1 type-section text-foreground">{selected.title}</h3>
          </div>
          <StatusPill tone={outcomeMeta.tone}>{outcomeMeta.label}</StatusPill>
        </div>
        {selected.summary && (
          <p className="mt-3 type-body text-foreground-muted">{selected.summary}</p>
        )}
        <dl className="mt-4 grid grid-cols-2 gap-4 border-y border-outline/20 py-4">
          <DataField label="Occurred" value={formatDateTime(selected.occurred_at)} />
          <DataField label="Source" value={selected.source_label ?? 'JARV1S'} />
          <DataField label="Delivery" value={selected.delivery ?? 'Not applicable'} />
          <DataField label="Updated" value={selected.updated_at ? formatRelativeWhen(selected.updated_at) : 'Not updated'} />
        </dl>
        <Suspense fallback={<Placeholder className="mt-4">Loading run detail…</Placeholder>}>
          <OperationRunDetail detailRef={selected.detail_ref} />
        </Suspense>
      </div>
    )
  }
  const hasSelection = category === 'conversation' ? Boolean(selectedConversation) : Boolean(selected)

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <StatusBarWorkspaceHeader
        title="Activity"
        titleId="operations-title"
        subtitle="Catch up on what ran, or manage what JARV1S has configured."
        onClose={closeOverlay}
        closeLabel="Close activity"
        trailing={
          <SegmentedTabs
            idPrefix="operations"
            label="Operations workspace"
            value={tab}
            onChange={setTab}
            className="order-3 w-full justify-center sm:order-none sm:w-auto"
            tabs={[
              { value: 'activity', label: 'Activity' },
              { value: 'configured', label: 'Configured' },
            ]}
          />
        }
      />

      <div
        id={`operations-panel-${tab}`}
        role="tabpanel"
        aria-labelledby={`operations-tab-${tab}`}
        className="flex min-h-0 flex-1 flex-col outline-none"
      >
        {tab === 'activity' ? (
          <>
            <div className="border-b border-outline/15 px-6 py-3">
              <div className="flex flex-wrap items-center gap-2">
                {categories.map((item) => (
                  <Chip
                    key={item.key}
                    selected={category === item.key}
                    onClick={() => {
                      setCategory(item.key)
                      setNodeId(null)
                      setSelected(null)
                      setSelectedConversation(null)
                      if (item.key === 'conversation' && outcome !== 'all' && outcome !== 'failed') {
                        setOutcome('all')
                      }
                    }}
                    className="min-h-10 type-label-small"
                  >
                    {item.label}
                  </Chip>
                ))}
                {hasActivityFilters && (
                  <Button
                    variant="ghost"
                    color="neutral"
                    size="xs"
                    className="ml-auto"
                    onClick={clearActivityFilters}
                    icon={<ArrowCounterClockwiseIcon size={14} />}
                  >
                    Reset filters
                  </Button>
                )}
              </div>
              <div className="mt-3 grid items-end gap-2 sm:grid-cols-[minmax(240px,1fr)_auto]">
                <SearchField
                  id="activity-search"
                  label={category === 'conversation' ? 'Search conversations' : 'Search activity'}
                  value={search}
                  onChange={setSearch}
                  placeholder={category === 'conversation' ? 'What was said' : 'Title, summary, or rule'}
                />
                <Button
                  variant="ghost"
                  color="subtle"
                  size="md"
                  shape="control"
                  className="w-full sm:w-auto"
                  aria-expanded={filtersExpanded}
                  aria-controls="activity-secondary-filters"
                  onClick={() => setFiltersExpanded((expanded) => !expanded)}
                  icon={<FunnelIcon size={15} />}
                >
                  Filters{secondaryFilterCount > 0 ? ` (${secondaryFilterCount})` : ''}
                </Button>
              </div>
              {filtersExpanded && (
                <div
                  id="activity-secondary-filters"
                  className="mt-3 grid gap-2 border-t border-outline/15 pt-3 sm:grid-cols-3"
                >
                  <FieldControl label="Status" htmlFor="activity-outcome">
                    <Select
                      id="activity-outcome"
                      aria-label="Filter activity by status"
                      value={outcome}
                      onChange={(value) => setOutcome(value as ActivityOutcome | 'all')}
                      options={category === 'conversation' ? conversationStatusFilters : outcomeOptions}
                    />
                  </FieldControl>
                  <FieldControl label="When" htmlFor="activity-time">
                    <Select
                      id="activity-time"
                      aria-label="Filter activity by time"
                      value={timeFilter}
                      onChange={(value) => setTimeFilter(value as TimeFilter)}
                      options={category === 'conversation' ? conversationTimeFilters : timeOptions}
                    />
                  </FieldControl>
                  <FieldControl label={category === 'conversation' ? 'Device' : 'Source'} htmlFor="activity-source">
                    <Input
                      id="activity-source"
                      value={source}
                      onChange={(event) => setSource(event.target.value)}
                      placeholder={category === 'conversation' ? 'Any device' : 'Any source'}
                    />
                  </FieldControl>
                </div>
              )}
            </div>
            <div className={cn(
              'grid min-h-0 flex-1',
              hasActivityResults && 'lg:grid-cols-[minmax(440px,1.05fr)_minmax(400px,0.95fr)]',
            )}>
              <div className={cn(
                'min-h-0 overflow-y-auto px-6 py-6',
                hasActivityResults && 'lg:border-r lg:border-outline/15',
                hasSelection && 'hidden lg:block',
              )}>
                {activityState === 'loading' && activity.length === 0 && <Placeholder>Loading activity…</Placeholder>}
                {activityState === 'error' && <Placeholder tone="error">Could not load activity.</Placeholder>}
                {activityState === 'ready' && activity.length === 0 && (
                  <EmptyState
                    title={hasActivityFilters
                      ? category === 'conversation' ? 'No conversations match' : 'No activity matches'
                      : 'No recent activity'}
                    description={
                      !hasActivityFilters
                        ? 'Reminders, automations, tasks, and system runs will appear here after JARV1S acts.'
                        : category === 'conversation'
                          ? 'Try another device, time range, or clear the current filters.'
                          : 'Try a broader time range or clear the current filters.'
                    }
                    action={hasActivityFilters
                      ? (
                          <Button
                            variant="ghost"
                            color="action"
                            size="sm"
                            onClick={clearActivityFilters}
                            icon={<ArrowCounterClockwiseIcon size={14} />}
                          >
                            Reset filters
                          </Button>
                        )
                      : undefined}
                  />
                )}
                <div className="space-y-5">
                  {(category === 'conversation' ? groupedConversations : groupedActivity).map((group) => (
                    <section key={group.label} aria-labelledby={`activity-${group.label.replace(/\W/g, '-').toLowerCase()}`}>
                      <h3 id={`activity-${group.label.replace(/\W/g, '-').toLowerCase()}`} className="mb-2 type-label text-foreground-subtle">
                        {group.label}
                      </h3>
                      <div className="space-y-1">
                        {category === 'conversation'
                          ? (group.items as ConversationSession[]).map((session) => (
                            <ConversationSessionRow
                              key={session.id}
                              session={session}
                              selected={selectedConversation?.id === session.id}
                              onSelect={setSelectedConversation}
                            />
                          ))
                          : (group.items as ActivityEntry[]).map((item) => (
                            <ActivityRow key={item.activity_id} item={item} selected={selected?.activity_id === item.activity_id} onClose={closeOverlay} onSelect={setSelected} />
                          ))}
                      </div>
                    </section>
                  ))}
                </div>
                {hasMore && nextCursor && (
                  <Button variant="ghost" color="subtle" size="md" className="mt-4 h-11 w-full" disabled={loadingMore} onClick={() => void loadActivity(nextCursor)}>
                    {loadingMore ? 'Loading…' : 'Load more'}
                  </Button>
                )}
              </div>
              <aside
                className={cn(
                  'min-h-0 flex-col overflow-y-auto bg-surface/[0.03]',
                  hasActivityResults
                    ? hasSelection ? 'flex' : 'hidden lg:flex'
                    : 'hidden',
                )}
                aria-label={category === 'conversation' ? 'Conversation detail' : 'Activity detail'}
              >
                {renderDetail()}
              </aside>
            </div>
          </>
        ) : (
          <>
            <div className="grid gap-2 border-b border-outline/15 px-6 py-3 sm:grid-cols-[180px_180px_minmax(240px,1fr)]">
              <FieldControl label="Type" htmlFor="configured-kind">
                <Select
                  id="configured-kind"
                  aria-label="Filter configured items by type"
                  value={setupKind}
                  onChange={(value) => setSetupKind(value as SetupKind | 'all')}
                  options={setupKinds.map((item) => ({ value: item.key, label: item.label }))}
                />
              </FieldControl>
              <FieldControl label="Status" htmlFor="configured-status">
                <Select
                  id="configured-status"
                  aria-label="Filter configured items by status"
                  value={setupStatus}
                  onChange={(value) => setSetupStatus(value as SetupStatus | 'all')}
                  options={setupStatusOptions}
                />
              </FieldControl>
              <SearchField
                id="configured-search"
                label="Search configured items"
                value={setupSearch}
                onChange={setSetupSearch}
                placeholder="Name or description"
              />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4">
              {(inboundStats?.dead_letter ?? 0) > 0 && (
                <PanelSection className="mb-4 border-status-warning/30 bg-status-warning/5 p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <h3 className="text-sm font-body text-foreground">Failed external events</h3>
                      <p className="mt-1 text-xs text-foreground-muted">
                        {inboundStats?.dead_letter} event
                        {(inboundStats?.dead_letter ?? 0) === 1 ? '' : 's'} exhausted retries. Replay to try again.
                      </p>
                    </div>
                    <StatusPill tone="warning">{inboundStats?.dead_letter} dead letter</StatusPill>
                  </div>
                  <div className="mt-3 space-y-2">
                    {deadLetters.map((event) => (
                      <div
                        key={event.id}
                        className="flex flex-wrap items-center justify-between gap-2 rounded-control border border-outline/20 bg-surface/20 px-3 py-2"
                      >
                        <div className="min-w-0">
                          <p className="font-mono text-[11px] uppercase tracking-[0.12em] text-foreground-subtle">
                            {event.kind} · {event.source}
                          </p>
                          <p className="mt-0.5 truncate text-xs text-foreground-muted">
                            {event.last_error || 'Processing failed'}
                          </p>
                        </div>
                        <Button
                          size="sm"
                          variant="ghost"
                          color="action"
                          disabled={retryingEventId === event.id}
                          onClick={() => void retryDeadLetter(event.id)}
                        >
                          {retryingEventId === event.id ? 'Retrying…' : 'Retry'}
                        </Button>
                      </div>
                    ))}
                  </div>
                </PanelSection>
              )}
              {setupState === 'loading' && <Placeholder>Loading configured items…</Placeholder>}
              {setupState === 'error' && <Placeholder tone="error">Could not load configured items.</Placeholder>}
              {setupState === 'ready' && setups.length === 0 && (
                <EmptyState
                  title="No configured items"
                  description="Try another type or status, or ask JARV1S to create a reminder or automation."
                />
              )}
              <div className="space-y-3">
                {setups.map((item) => {
                  const busy = busySetups.has(item.resource_ref)
                  const canPause = item.supported_actions.includes('pause')
                  const canResume = item.supported_actions.includes('resume')
                  const canDelete = item.supported_actions.includes('delete')
                  const displayName = displaySetupName(item.name)
                  const confirming = confirmingDelete === item.resource_ref
                  const detailsOpen = expandedSetups.has(item.resource_ref)
                  if (confirming) {
                    return (
                      <PanelSection
                        key={item.resource_ref}
                        as="article"
                        className="border border-status-danger/20 bg-status-danger/[0.06] p-4"
                      >
                        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                          <div className="min-w-0">
                            <h3 className="type-heading text-foreground">Delete {displayName}?</h3>
                            <p className="mt-1 type-body text-foreground-muted">
                              This configured item and its future runs will be removed permanently.
                            </p>
                          </div>
                          <div className="flex shrink-0 flex-wrap gap-2">
                            <Button
                              color="critical"
                              size="sm"
                              disabled={busy}
                              onClick={() => void deleteSetup(item)}
                              icon={<TrashIcon size={14} />}
                            >
                              {busy ? 'Deleting…' : 'Delete permanently'}
                            </Button>
                            <Button
                              variant="ghost"
                              color="neutral"
                              size="sm"
                              disabled={busy}
                              onClick={() => setConfirmingDelete(null)}
                            >
                              Cancel
                            </Button>
                          </div>
                        </div>
                        {setupErrors[item.resource_ref] && (
                          <p role="alert" className="mt-3 type-meta text-status-danger-fg">
                            {setupErrors[item.resource_ref]}
                          </p>
                        )}
                      </PanelSection>
                    )
                  }
                  return (
                    <PanelSection key={item.resource_ref} as="article" className="p-4">
                      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                        <div className="min-w-0">
                          <div className="flex flex-wrap items-center gap-2">
                            <h3 className="type-heading text-foreground">{displayName}</h3>
                            <StatusPill tone={item.status === 'active' ? 'success' : item.status === 'paused' ? 'warning' : 'neutral'}>{item.status}</StatusPill>
                            <StatusPill tone="neutral">{item.setup_type.replace(/_/g, ' ')}</StatusPill>
                          </div>
                          {item.description && <p className="mt-2 max-w-2xl line-clamp-2 type-body text-foreground-muted">{item.description}</p>}
                          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 type-meta tabular-nums text-foreground-subtle">
                            <span>Next: {formatDateTime(item.next_due_at)}</span>
                            <span>Last: {formatDateTime(item.last_run_at)}</span>
                            {item.last_outcome && <span>Outcome: {item.last_outcome}</span>}
                          </div>
                        </div>
                        <div className="flex shrink-0 flex-wrap items-center gap-2">
                          <Button variant="ghost" color="action" size="sm" className="h-10" onClick={() => viewSetupActivity(item)}>
                            View activity
                          </Button>
                          <Button
                            variant="ghost"
                            color={detailsOpen ? 'brand' : 'neutral'}
                            size="sm"
                            className="h-10"
                            aria-expanded={detailsOpen}
                            onClick={() => setExpandedSetups((current) => {
                              const next = new Set(current)
                              if (next.has(item.resource_ref)) next.delete(item.resource_ref)
                              else next.add(item.resource_ref)
                              return next
                            })}
                            icon={(
                              <CaretDownIcon
                                size={14}
                                className={cn(
                                  'transition-transform duration-feedback motion-reduce:transition-none',
                                  detailsOpen && 'rotate-180',
                                )}
                              />
                            )}
                          >
                            Details
                          </Button>
                          {item.status === 'active' && canPause && (
                            <Button
                              variant="ghost"
                              color="subtle"
                              size="sm"
                              className="h-10"
                              disabled={busy}
                              onClick={() => void patchSetup(item, { enabled: false })}
                              icon={<PauseIcon size={14} />}
                            >
                              Pause
                            </Button>
                          )}
                          {item.status !== 'active' && canResume && (
                            <Button
                              variant="ghost"
                              color="subtle"
                              size="sm"
                              className="h-10"
                              disabled={busy}
                              onClick={() => void patchSetup(item, { enabled: true })}
                              icon={<PlayIcon size={14} />}
                            >
                              Resume
                            </Button>
                          )}
                          {canDelete && (
                            <Button
                              variant="ghost"
                              color="danger"
                              size="sm"
                              className="h-10"
                              disabled={busy}
                              onClick={() => setConfirmingDelete(item.resource_ref)}
                              icon={<TrashIcon size={14} />}
                            >
                              Delete
                            </Button>
                          )}
                        </div>
                      </div>
                      {detailsOpen && (
                        <dl className="mt-4 grid grid-cols-2 gap-4 border-t border-outline/15 pt-4 sm:grid-cols-4">
                          <DataField label="Source" value={item.source_label} />
                          <DataField label="Trigger" value={item.trigger_label} />
                          <DataField label="Cadence" value={item.cadence_label ?? 'On demand'} />
                          <DataField label="Action" value={item.action_label} />
                        </dl>
                      )}
                      {setupErrors[item.resource_ref] && <p role="alert" className="mt-3 text-[12px] font-body text-status-danger">{setupErrors[item.resource_ref]}</p>}
                    </PanelSection>
                  )
                })}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
