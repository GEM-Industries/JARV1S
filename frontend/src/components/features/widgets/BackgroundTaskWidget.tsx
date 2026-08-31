import React, { useState, useEffect, useRef } from 'react';
import {
  RobotIcon,
  CheckCircleIcon,
  XCircleIcon,
  CircleNotchIcon,
  WarningIcon,
  FileTextIcon,
  ArrowSquareOutIcon,
  ProhibitIcon,
} from '@phosphor-icons/react';
import { cn } from '../../../utils/cn';
import { authorizedFetch } from '../../../client/http';
import { jarvisClient } from '../../../client/JarvisClient';
import { Button } from '../../ui/Button';
import { Disclosure } from '../../ui/Disclosure';
import { MarkdownContent } from './content/MarkdownContent';
import {
  WidgetBody,
  WidgetHeader,
  WidgetMetaPill,
  WidgetPanel,
  WidgetRow,
} from './primitives';
import { WidgetDefinition, BaseWidgetProps } from './types';
import { describeBatch, presentTaskTrace, type TaskTraceItem } from './taskTrace';

interface TaskArtifact {
  path: string;
  source: 'code' | 'jarvis' | string;
  exists_verified: boolean;
  exists?: boolean | null;
  size_bytes?: number | null;
  changed?: boolean | null;
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
  title?: string | null;
  status: 'running' | 'completed' | 'failed' | 'cancelled';
  progress_summary: string;
  live_status?: string | null;
  attention?: 'none' | 'approval' | 'question' | string;
  pending_input?: PendingInputSummary | null;
  source: string;
  mode?: 'code' | 'jarvis' | string | null;
  session_id?: string | null;
  worker_kind?: 'claude_code' | 'cursor_local' | string | null;
  created_at: string;
  cwd?: string | null;
  artifacts?: TaskArtifact[];
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
  cancelled: 'text-foreground-muted',
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

const openWork = (taskId: string, path?: string) => {
  jarvisClient.sendMessage('ui.action', {
    plugin: 'agents',
    tool: 'inspect',
    args: path ? { target: taskId, path } : { target: taskId },
  });
};

const ArtifactRow: React.FC<{
  artifact: TaskArtifact;
  onOpen?: (path: string) => void;
}> = ({ artifact, onOpen }) => {
  const filename = fileNameFromPath(artifact.path);
  const directory = directoryFromPath(artifact.path);
  const missing = artifact.exists_verified && !artifact.exists;
  const canOpen = Boolean(onOpen) && !missing;

  return (
    <button
      type="button"
      title={canOpen ? `Open ${filename}` : artifact.path}
      disabled={!canOpen}
      onClick={() => onOpen?.(artifact.path)}
      className={cn(
        'ui-surface-selectable flex min-h-10 w-full items-center gap-3 px-3 py-2 text-left',
        canOpen ? 'cursor-pointer' : 'cursor-default opacity-70',
      )}
    >
      <FileTextIcon
        size={16}
        weight="light"
        className={cn('shrink-0', missing ? 'text-status-warning' : 'text-brand')}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate type-body text-foreground">{filename}</span>
        {directory && (
          <span className="block truncate type-meta text-foreground-subtle">{directory}</span>
        )}
      </span>
      {canOpen && (
        <ArrowSquareOutIcon size={14} className="shrink-0 text-foreground-subtle" aria-hidden />
      )}
    </button>
  );
};

const BackgroundTaskHero: React.FC<BackgroundTaskData & BaseWidgetProps> = ({
  task_id,
  title,
  status,
  progress_summary,
  live_status,
  attention = 'none',
  pending_input,
  source,
  mode,
  session_id: initialSessionId,
  worker_kind,
  created_at,
  cwd: initialCwd,
  artifacts: initialArtifacts = [],
  trace: initialTrace = [],
}) => {
  const [fullResult, setFullResult] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>(initialArtifacts);
  const [trace, setTrace] = useState<TaskTraceItem[]>(initialTrace);
  const [durationMs, setDurationMs] = useState<number | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId ?? null);
  const [cwd, setCwd] = useState<string | null>(initialCwd ?? null);
  const [loadError, setLoadError] = useState(false);
  const fetchedDetailKey = useRef<string | null>(null);

  const isRunning = status === 'running';
  const isDone = status === 'completed' || status === 'failed' || status === 'cancelled';
  const elapsed = useElapsed(created_at, isRunning);
  const isCursor = worker_kind === 'cursor_local';

  useEffect(() => {
    if (initialArtifacts.length > 0) setArtifacts(initialArtifacts);
    if (initialTrace.length > 0) setTrace(initialTrace);
    if (initialCwd) setCwd(initialCwd);
  }, [initialArtifacts, initialTrace, initialCwd]);

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
        setTrace(doc?.trace ?? []);
        setFullResult(doc?.result || null);
        setDurationMs(doc?.duration_ms ?? null);
        if (typeof doc?.session_id === 'string' && doc.session_id) {
          setSessionId(doc.session_id);
        }
        if (typeof doc?.cwd === 'string' && doc.cwd) {
          setCwd(doc.cwd);
        }
      })
      .catch(() => setLoadError(true));
  }, [isDone, task_id, status]);

  const resultText = fullResult ?? (isDone ? progress_summary : null);
  const statusLabel =
    status === 'running' ? 'Running'
      : status === 'completed' ? 'Complete'
        : status === 'cancelled' ? 'Cancelled'
          : 'Failed';
  const durationLabel = formatDuration(durationMs);

  const statusIcon = isRunning ? (
    <CircleNotchIcon size={16} className="text-status-warning animate-spin" />
  ) : status === 'completed' ? (
    <CheckCircleIcon size={16} weight="fill" className="text-status-success" />
  ) : status === 'cancelled' ? (
    <ProhibitIcon size={16} weight="fill" className="text-foreground-muted" />
  ) : (
    <XCircleIcon size={16} weight="fill" className="text-status-danger" />
  );

  const workLog = presentTaskTrace(trace);
  const canOpenProject =
    mode !== 'jarvis'
    && Boolean(cwd)
    && (isCursor || (Boolean(sessionId) && !isRunning));
  const canOpenFiles = mode !== 'jarvis';
  const inspectTitle = isCursor
    ? 'Open in Cursor'
    : worker_kind === 'claude_code'
      ? 'Open in Claude Code'
      : 'Open';

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <WidgetHeader
        title={title?.trim() || 'Working'}
        subtitle={statusLabel}
        leading={statusIcon}
        trailing={
          <span className="flex items-center gap-2">
            {canOpenProject && (
              <Button
                type="button"
                variant="ghost"
                color="subtle"
                size="xs"
                shape="control"
                icon={<ArrowSquareOutIcon size={14} />}
                aria-label={inspectTitle}
                onClick={() => openWork(task_id)}
              >
                Open
              </Button>
            )}
            <span className="type-meta tabular-nums text-foreground-subtle">{durationLabel || elapsed}</span>
          </span>
        }
        meta={mode ? <WidgetMetaPill>{mode}</WidgetMetaPill> : undefined}
      />

      {isRunning && (
        <div className="shrink-0 px-5 pt-3">
          <WidgetPanel tone="warning" className="rounded-control px-3 py-2">
            <p className="line-clamp-1 type-meta text-foreground-muted">
              {live_status || progress_summary || 'Running…'}
            </p>
          </WidgetPanel>
        </div>
      )}

      <WidgetBody className="space-y-6">
        {attention === 'approval' && pending_input && (
          <WidgetPanel title="Needs approval" icon={<WarningIcon size={12} />}>
            <WidgetRow
              compact
              icon={<WarningIcon size={12} className="text-status-warning" />}
              label={<span className="text-status-warning">Waiting on you</span>}
              description={pending_input.prompt}
            />
            {pending_input.detail && (
              <div className="mt-1 line-clamp-2 type-meta text-foreground-subtle">{pending_input.detail}</div>
            )}
          </WidgetPanel>
        )}

        {resultText && (
          <MarkdownContent content={resultText} className="type-body text-foreground" />
        )}

        {artifacts.length > 0 && (
          <section>
            <div className="mb-2 flex items-baseline justify-between gap-3">
              <h3 className="type-label-small text-foreground-subtle">
                {artifacts.length === 1 ? 'File' : 'Files'}
              </h3>
              <span className="type-meta text-foreground-subtle">{artifacts.length}</span>
            </div>
            <div className="ui-surface-group">
              {artifacts.map((artifact) => (
                <ArtifactRow
                  key={artifact.path}
                  artifact={artifact}
                  onOpen={canOpenFiles ? (path) => openWork(task_id, path) : undefined}
                />
              ))}
            </div>
          </section>
        )}

        {workLog.length > 0 && (
          <Disclosure
            label={<span className="type-label-small text-foreground-muted">Work log</span>}
            summaryClassName="min-h-10"
            trailing={<span className="type-meta text-foreground-subtle">{workLog.length}</span>}
            contentClassName="mt-2 max-h-72 space-y-4 overflow-y-auto scrollbar-thin pr-1"
          >
            {workLog.map((entry) => {
              if (entry.kind === 'reply') {
                return (
                  <p
                    key={entry.key}
                    className={cn(
                      'border-l border-outline-subtle pl-3 type-body text-foreground-muted',
                      entry.thinking && 'italic text-foreground-subtle',
                    )}
                  >
                    {entry.text}
                  </p>
                );
              }
              if (entry.kind === 'error') {
                return (
                  <p key={entry.key} className="type-meta text-status-danger">
                    {entry.text}
                  </p>
                );
              }
              return (
                <p
                  key={entry.key}
                  className={cn(
                    'type-meta text-foreground-subtle',
                    entry.failed && 'text-status-danger',
                  )}
                >
                  {describeBatch(entry)}
                </p>
              );
            })}
          </Disclosure>
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
          <span className="type-fui text-foreground-subtle">{source}</span>
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
      : data.status === 'running' ? '…'
        : data.status === 'completed' ? 'Done'
          : data.status === 'cancelled' ? 'Cancelled'
            : 'Failed',
    labelVariant: 'mono',
    width: 'wide',
  }),
};
