export type ActivityCategory = 'conversation' | 'reminder' | 'automation' | 'task' | 'system'
export type ActivityOutcome = 'succeeded' | 'failed' | 'waiting' | 'running' | 'suppressed' | 'cancelled'
export type ActivityDetailKind = 'turn' | 'trigger_instance' | 'background_task'

export interface ActivityDetailRef {
  kind: ActivityDetailKind
  id: string
}

export interface ActivityEntry {
  activity_id: string
  category: ActivityCategory
  occurred_at: string
  updated_at?: string | null
  outcome: ActivityOutcome
  title: string
  summary?: string | null
  source_key?: string | null
  source_label?: string | null
  delivery?: 'announce' | 'silent' | 'suppressed' | 'evaluate' | 'prefetched' | null
  detail_ref: ActivityDetailRef
  turn_id?: string | null
  instance_id?: string | null
  task_id?: string | null
  rule_id?: string | null
  node_id?: string | null
  failure_label?: string | null
}

export interface ActivityPage {
  items: ActivityEntry[]
  next_cursor?: string | null
  has_more: boolean
}

/** Compatibility shape used by the compact activity menu. */
export interface ActivityItem extends ActivityEntry {
  kind: 'headless' | 'task' | 'trigger' | 'automation' | 'user'
  id: string
  sort_at: string
}

export interface OperationTraceLine {
  timestamp: string;
  role: string;
  content: string;
  turn_type?: string | null;
  tool_call_id?: string | null;
  code?: string | null;
  output?: string | null;
}

export interface OperationPerfStage {
  key: string;
  label?: string | null;
  detail?: string | null;
  ms?: number | null;
  group?: string | null;
  status?: string | null;
}

export interface OperationPerfSummary {
  status?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  response_ms?: number | null;
  total_ms?: number | null;
  model?: string | null;
  stages: OperationPerfStage[];
  stt?: Record<string, unknown> | null;
  turn_detection?: Record<string, unknown> | null;
  voice?: Record<string, unknown> | null;
  tool_routing?: Record<string, unknown> | null;
}

export interface OperationProtocolRun {
  protocol_name: string;
  triggered_by?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  status: string;
}

export interface OperationTurnAttempt {
  turn_id: string;
  trace: OperationTraceLine[];
  perf?: OperationPerfSummary | null;
  protocols: OperationProtocolRun[];
}

export interface OperationRunDetail {
  id: string;
  kind: 'trigger' | 'automation' | 'user' | 'system';
  owner_id: string;
  status: string;
  rule_id?: string | null;
  source?: string | null;
  due_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  started_at?: string | null;
  result_text?: string | null;
  failure_reason?: string | null;
  node_id?: string | null;
  node_label?: string | null;
  modality?: string | null;
  origin_snapshot: Record<string, unknown>;
  action_snapshot: Record<string, unknown>;
  source_event: Record<string, unknown>;
  turn_ids: string[];
  attempts: OperationTurnAttempt[];
}
