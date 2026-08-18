import React, { useState, useEffect, useRef } from 'react';
import {
  RobotIcon,
  CheckCircleIcon,
  XCircleIcon,
  CircleNotchIcon,
  TerminalIcon,
  WarningIcon,
  FileTextIcon,
} from '@phosphor-icons/react';
import { cn } from '../../../utils/cn';
import { authorizedFetch } from '../../../client/http';
import { Disclosure } from '../../ui/Disclosure';
import { MarkdownContent } from './content/MarkdownContent';
import {
  WidgetBody,
  WidgetCard,
  WidgetEyebrow,
  WidgetHeader,
  WidgetMetaPill,
  WidgetPanel,
  WidgetRow,
} from './primitives';
import { WidgetDefinition, BaseWidgetProps } from './types';

interface TaskArtifact {
  path: string;
  source: 'code' | 'jarvis' | string;
  exists_verified: boolean;
  exists?: boolean | null;
  size_bytes?: number | null;
  changed?: boolean | null;
}

interface TaskActivity {
  source: 'code' | 'jarvis' | string;
  status: 'completed' | 'failed' | string;
  summary: string;
}

interface TaskTraceItem {
  kind: 'tool_call' | 'tool_result' | 'text' | 'reasoning' | 'ui' | 'artifact' | 'error' | string;
  ts: number;
  span_id?: string | null;
  parent_id?: string | null;
  tool?: string | null;
  code?: string | null;
  args_preview?: Record<string, unknown> | null;
  text_preview?: string | null;
  result_preview?: string | null;
  status?: 'running' | 'completed' | 'failed' | string | null;
}

interface PendingInputSummary {
  input_id: string;
  kind: 'approval' | string;
  status: 'pending' | 'approved' | 'denied' | 'expired' | 'cancelled' | string;
  prompt: string;
  detail?: string | null;
  risk?: 'low' | 'medium' | 'high' | string | null;
  widget_id?: string | null;
}

interface BackgroundTaskData {
  task_id: string;
  status: 'running' | 'completed' | 'failed';
  progress_summary: string;
  live_status?: string | null;
  attention?: 'none' | 'approval' | 'question' | string;
  pending_input?: PendingInputSummary | null;
  source: string;
  mode?: string | null;
  created_at: string;
  artifacts?: TaskArtifact[];
  activity?: TaskActivity[];
  trace?: TaskTraceItem[];
}

interface TaskDetailResponse extends BackgroundTaskData {
  cwd: string;
  max_turns: number;
  max_budget_usd: number;
  result?: string | null;
  session_id?: string | null;
  duration_ms?: number | null;
  num_turns?: number | null;
  events?: unknown[];
}

const STATUS_COLORS: Record<string, string> = {
  running: 'text-status-warning',
  completed: 'text-status-success',
  failed: 'text-status-danger',
};

const ACTIVITY_STATUS_COLORS: Record<string, string> = {
  completed: 'text-status-success',
  failed: 'text-status-danger',
};

function useElapsed(createdAt: string, active: boolean): string {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);

  const createdAtMs = Date.parse(createdAt);
  if (Number.isNaN(createdAtMs)) return '0s';
  const secs = Math.floor((now - createdAtMs) / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}

const formatDuration = (ms?: number | null): string | null => {
  if (ms == null) return null;
  const secs = Math.round(ms / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const rem = secs % 60;
  return rem ? `${mins}m ${rem}s` : `${mins}m`;
};

const fileNameFromPath = (path: string): string => path.split('/').filter(Boolean).pop() || path;

const directoryFromPath = (path: string): string => {
  const parts = path.split('/').filter(Boolean);
  if (parts.length <= 1) return '';
  const dir = `/${parts.slice(0, -1).join('/')}`;
  return dir.length > 48 ? `…${dir.slice(-47)}` : dir;
};

const traceSummary = (item: TaskTraceItem): string => {
  if (item.tool) return item.tool;
  if (item.result_preview) return item.result_preview;
  if (item.text_preview) return item.text_preview;
  if (item.code) return item.code;
  return item.kind;
};

const ArtifactRow: React.FC<{ artifact: TaskArtifact }> = ({ artifact }) => {
  const filename = fileNameFromPath(artifact.path);
  const directory = directoryFromPath(artifact.path);
  const missing = artifact.exists_verified && !artifact.exists;

  return (
    <WidgetCard className="flex items-center gap-3 px-3 py-2.5" title={artifact.path}>
      <FileTextIcon
        size={16}
        weight="light"
        className={cn('shrink-0', missing ? 'text-status-warning' : 'text-status-success')}
      />
      <div className="min-w-0 flex-1">
        <div className="truncate font-mono text-[12px] text-foreground">{filename}</div>
        {directory && (
          <div className="truncate font-mono text-[10px] text-foreground-muted/50">{directory}</div>
        )}
      </div>
    </WidgetCard>
  );
};

const BackgroundTaskHero: React.FC<BackgroundTaskData & BaseWidgetProps> = ({
  task_id,
  status,
  progress_summary,
  live_status,
  attention = 'none',
  pending_input,
  source,
  mode,
  created_at,
  artifacts: initialArtifacts = [],
  activity: initialActivity = [],
  trace: initialTrace = [],
}) => {
  const [fullResult, setFullResult] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>(initialArtifacts);
  const [activity, setActivity] = useState<TaskActivity[]>(initialActivity);
  const [trace, setTrace] = useState<TaskTraceItem[]>(initialTrace);
  const [durationMs, setDurationMs] = useState<number | null>(null);
  const [loadError, setLoadError] = useState(false);
  const fetchedDetailKey = useRef<string | null>(null);

  const isRunning = status === 'running';
  const isDone = status === 'completed' || status === 'failed';
  const elapsed = useElapsed(created_at, isRunning);

  useEffect(() => {
    if (initialArtifacts.length > 0) setArtifacts(initialArtifacts);
    if (initialActivity.length > 0) setActivity(initialActivity);
    if (initialTrace.length > 0) setTrace(initialTrace);
  }, [initialArtifacts, initialActivity, initialTrace]);

  useEffect(() => {
    if (!isDone) return;
    const fetchKey = `${task_id}:${status}`;
    if (fetchedDetailKey.current === fetchKey) return;
    fetchedDetailKey.current = fetchKey;
    setLoadError(false);
    authorizedFetch(`/api/v1/tasks/${task_id}`)
      .then((r) => {
        if (!r.ok) throw new Error();
        return r.json();
      })
      .then((doc: TaskDetailResponse) => {
        setArtifacts(doc?.artifacts ?? []);
        setActivity(doc?.activity ?? []);
        setTrace(doc?.trace ?? []);
        setFullResult(doc?.result || null);
        setDurationMs(doc?.duration_ms ?? null);
      })
      .catch(() => setLoadError(true));
  }, [isDone, task_id, status]);

  const resultText = fullResult ?? (isDone ? progress_summary : null);
  const statusLabel = status === 'running' ? 'Running' : status === 'completed' ? 'Complete' : 'Failed';
  const durationLabel = formatDuration(durationMs);

  const statusIcon = isRunning ? (
    <CircleNotchIcon size={16} className="text-status-warning animate-spin" />
  ) : status === 'completed' ? (
    <CheckCircleIcon size={16} weight="fill" className="text-status-success" />
  ) : (
    <XCircleIcon size={16} weight="fill" className="text-status-danger" />
  );

  return (
    <div className="flex h-full select-none flex-col overflow-hidden">
      <WidgetHeader
        title="Background Task"
        subtitle={statusLabel}
        leading={statusIcon}
        trailing={
          <span className="text-[11px] font-mono text-foreground-muted">{durationLabel || elapsed}</span>
        }
        meta={mode ? <WidgetMetaPill>{mode}</WidgetMetaPill> : undefined}
      />

      {isRunning && (
        <div className="shrink-0 px-5 pt-3">
          <WidgetPanel tone="warning" className="rounded-control px-3 py-2">
            <p className="line-clamp-1 text-xs font-mono text-foreground-muted">
              {live_status || progress_summary || 'Running…'}
            </p>
          </WidgetPanel>
        </div>
      )}

      <WidgetBody className="space-y-3">
        {attention === 'approval' && pending_input && (
          <WidgetPanel title="Needs Approval" icon={<WarningIcon size={12} />}>
            <WidgetRow
              compact
              icon={<WarningIcon size={12} className="text-status-warning" />}
              label={<span className="text-status-warning">waiting on you</span>}
              description={pending_input.prompt}
            />
            {pending_input.detail && (
              <div className="mt-1 line-clamp-2 font-mono text-[10px] opacity-60">{pending_input.detail}</div>
            )}
          </WidgetPanel>
        )}

        {resultText && <MarkdownContent content={resultText} className="text-[13px]" />}

        {artifacts.length > 0 && (
          <WidgetPanel
            title={artifacts.length === 1 ? 'File' : 'Files'}
            action={
              <WidgetEyebrow muted className="text-foreground-muted/50">
                {artifacts.length}
              </WidgetEyebrow>
            }
          >
            {artifacts.map((artifact) => (
              <ArtifactRow key={artifact.path} artifact={artifact} />
            ))}
          </WidgetPanel>
        )}

        {activity.length > 0 && (
          <WidgetPanel title="Activity" icon={<TerminalIcon size={12} />}>
            {activity.slice(-5).map((item, i) => (
              <WidgetRow
                key={`${item.source}-${item.status}-${i}`}
                compact
                icon={
                  <TerminalIcon
                    size={11}
                    className={cn(ACTIVITY_STATUS_COLORS[item.status] ?? 'text-outline')}
                  />
                }
                label={item.source}
                description={item.summary}
              />
            ))}
          </WidgetPanel>
        )}

        {trace.length > 0 && (
          <WidgetPanel>
            <Disclosure
              label={(
                <span className="inline-flex items-center gap-2">
                  <TerminalIcon size={12} />
                  <WidgetEyebrow muted size="md">
                    Evidence
                  </WidgetEyebrow>
                </span>
              )}
              trailing={<span className="text-foreground-muted/40">{trace.length}</span>}
              contentClassName="mt-2 space-y-1.5"
            >
                {trace.map((item, i) => (
                  <WidgetRow
                    key={`${item.kind}-${item.ts}-${i}`}
                    compact
                    className="rounded-md px-1 py-0.5"
                    icon={
                      <TerminalIcon
                        size={11}
                        className={cn(
                          item.kind === 'reasoning'
                            ? 'text-foreground-muted/50'
                            : item.status === 'failed'
                              ? 'text-status-danger'
                              : 'text-outline',
                        )}
                      />
                    }
                    label={
                      <span className={item.kind === 'reasoning' ? 'text-foreground-muted/70' : undefined}>
                        {item.kind}
                        {item.status ? ` / ${item.status}` : ''}
                      </span>
                    }
                    description={traceSummary(item)}
                    descriptionClassName={item.kind === 'reasoning' ? 'italic opacity-80' : undefined}
                  />
                ))}
            </Disclosure>
          </WidgetPanel>
        )}

        {loadError && (
          <WidgetRow
            compact
            icon={<WarningIcon size={11} className="text-status-danger" />}
            label={<span className="text-status-danger">Failed to load detail.</span>}
          />
        )}
      </WidgetBody>

      {isDone && source !== 'voice' && (
        <div className="flex shrink-0 justify-end border-t border-outline-subtle/40 px-5 py-3">
          <span className="text-[10px] font-mono uppercase tracking-wide text-foreground-muted opacity-60">
            {source}
          </span>
        </div>
      )}
    </div>
  );
};

export const BackgroundTaskWidget: WidgetDefinition<BackgroundTaskData> = {
  Hero: BackgroundTaskHero,
  getCompressedConfig: (data) => ({
    icon: (
      <RobotIcon
        size={20}
        weight="fill"
        className={cn(STATUS_COLORS[data.status] ?? 'text-foreground-muted')}
      />
    ),
    label: data.attention === 'approval'
      ? 'Approval'
      : data.status === 'running' ? '…' : data.status === 'completed' ? 'Done' : 'Failed',
    labelVariant: 'mono',
    width: 'wide',
  }),
};
