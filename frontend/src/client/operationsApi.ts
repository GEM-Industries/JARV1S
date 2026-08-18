import { authorizedFetch } from './http'

import type {
  ActivityCategory,
  ActivityEntry,
  ActivityItem,
  ActivityOutcome,
  ActivityPage,
  OperationRunDetail,
} from '../types/operations'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authorizedFetch(`/api/v1${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const json = await res.json()
      detail = json.detail ?? detail
    } catch {
      // ignore parse errors
    }
    throw new Error(detail)
  }

  if (res.status === 204) {
    return undefined as T
  }

  return res.json() as Promise<T>
}

export type SetupKind =
  | 'automation'
  | 'schedule'
  | 'deferred_instruction'
  | 'reminder'
  | 'timer'
  | 'alarm'
  | 'protocol'

export type SetupStatus = 'active' | 'disabled' | 'paused'
export type SetupType =
  | 'schedule'
  | 'automation'
  | 'habit_checkin'
  | 'quiet_window'
  | 'protocol'
  | 'scheduled_occurrence'
export type SetupAction = 'pause' | 'resume' | 'delete'

export interface ManagedSetup {
  resource_ref: string
  resource_id: string
  setup_type: SetupType
  managed_by: string
  scope: 'definition' | 'occurrence'
  kind: SetupKind
  name: string
  description?: string | null
  status: SetupStatus
  supported_actions: SetupAction[]
  edit_tool?: string | null
  series_id?: string | null
  rule_id?: string | null
  instance_id?: string | null
  next_due_at?: string | null
  last_run_at?: string | null
  last_outcome?: string | null
  paused_until?: string | null
  source_label: string
  trigger_label: string
  cadence_label?: string | null
  action_label: string
}

export interface ActivityPageParams {
  limit?: number
  cursor?: string | null
  category?: ActivityCategory | null
  outcome?: ActivityOutcome | null
  source?: string | null
  node_id?: string | null
  since?: string | null
  until?: string | null
  search?: string | null
}

export interface SetupListParams {
  kind?: SetupKind | null
  status?: SetupStatus | null
  setup_type?: SetupType | null
  search?: string | null
}

export interface SetupPatch {
  enabled?: boolean
  paused_until?: string | null
}

function queryString(values: Record<string, string | number | null | undefined>): string {
  const params = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') params.set(key, String(value))
  })
  const query = params.toString()
  return query ? `?${query}` : ''
}

function legacyActivityItem(entry: ActivityEntry): ActivityItem {
  const kind: ActivityItem['kind'] = entry.category === 'conversation'
    ? 'user'
    : entry.category === 'reminder'
      ? 'trigger'
      : entry.category === 'automation'
        ? 'automation'
        : entry.category === 'task'
          ? 'task'
          : 'headless'
  return {
    ...entry,
    kind,
    id: entry.detail_ref.id,
    sort_at: entry.occurred_at,
  }
}

export const operationsApi = {
  activityPage(params: ActivityPageParams = {}): Promise<ActivityPage> {
    return request<ActivityPage>(`/activity/page${queryString({
      limit: params.limit ?? 50,
      cursor: params.cursor,
      category: params.category,
      outcome: params.outcome,
      source: params.source,
      node_id: params.node_id,
      since: params.since,
      until: params.until,
      search: params.search,
    })}`)
  },

  setups(params: SetupListParams = {}): Promise<ManagedSetup[]> {
    return request<ManagedSetup[]>(`/operations/setups${queryString({
      kind: params.kind,
      status: params.status,
      setup_type: params.setup_type,
      search: params.search,
    })}`)
  },

  patchSetup(setupRef: string, patch: SetupPatch): Promise<ManagedSetup> {
    return request<ManagedSetup>(`/operations/setups/${encodeURIComponent(setupRef)}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    })
  },

  deleteSetup(setupRef: string): Promise<void> {
    return request<void>(`/operations/setups/${encodeURIComponent(setupRef)}`, {
      method: 'DELETE',
    })
  },

  /** Compatibility helper for the compact activity menu. All excludes conversations. */
  activity(
    kind?: ActivityItem['kind'] | 'all',
    limit = 100,
  ): Promise<ActivityItem[]> {
    const category: ActivityCategory | undefined = kind === 'user'
      ? 'conversation'
      : kind === 'trigger'
        ? 'reminder'
        : kind === 'automation'
          ? 'automation'
          : kind === 'task'
            ? 'task'
            : kind === 'headless'
              ? 'system'
              : undefined
    return this.activityPage({ category, limit }).then((page) => page.items.map(legacyActivityItem))
  },

  userTurns(nodeId?: string | null, limit = 100): Promise<ActivityItem[]> {
    const params = new URLSearchParams({ limit: String(limit) })
    if (nodeId) params.set('node_id', nodeId)
    return request<ActivityItem[]>(`/operations/turns?${params.toString()}`)
  },

  runDetail(instanceId: string): Promise<OperationRunDetail> {
    return request<OperationRunDetail>(`/operations/runs/${encodeURIComponent(instanceId)}`)
  },

  userTurnDetail(turnId: string): Promise<OperationRunDetail> {
    return request<OperationRunDetail>(`/operations/turns/${encodeURIComponent(turnId)}`)
  },
}
