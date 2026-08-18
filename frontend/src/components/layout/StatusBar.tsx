import React, { useState, useEffect, useCallback } from 'react';
import { WifiHighIcon, GearIcon, PulseIcon, HouseIcon, ArrowsClockwiseIcon, ChatTextIcon, BugIcon, XCircleIcon, ClockCounterClockwiseIcon, PuzzlePieceIcon, DownloadSimpleIcon } from '@phosphor-icons/react';
import { RecentActivityPopover } from './ActivityMenu';
import { cn } from '../../utils/cn';
import { useJarvisStore, type ContextMetrics, type Diagnostics } from '../../store/useJarvisStore';
import { jarvisClient } from '../../client/JarvisClient';
import { Divider } from '../ui/Divider';
import { Hologram } from '../ui/Hologram';
import { TacticalButton } from '../ui/TacticalButton';
import {
  StatusBarSurfaceHost,
  type StatusBarSurface,
} from '../ui/StatusBarSurfaceHost';
import {
  HolographicMenu as FloatingStatusBarMenu,
  MenuInfoRow as MenuInfo,
  MenuItem,
  MenuSectionHeader as MenuHeader,
  StatusBarMenuContent,
} from '../ui/holographic-menu';
import { IntegrationsPanelContent } from '../features/integrations/IntegrationsPanel';
import { SettingsPanelContent } from '../features/settings/SettingsPanel';
import { HomePanelContent } from '../features/home/HomePanel';
import { HomeAssistantPanelContent } from '../features/smart-home/HomeAssistantPanel';
import { OperationsPanelContent } from '../features/operations/OperationsPanel';
import { PresencePanelContent } from '../features/presence/PresencePanel';
import { getSystemStatus, resolveDashboardStatus } from '../../config/systemStatus';
import { authorizedFetch } from '../../client/http';
import { exportDiagnosticsBundle } from '../../runtime/desktopBridge';
import { isDesktopApp } from '../../runtime/clientSurface';

// --- DIAGNOSTICS HELPERS ---

const thresholdClass = (val: number | null | undefined, warn: number, crit: number): string =>
  val == null ? 'text-foreground-disabled' : val < warn ? 'text-status-success' : val < crit ? 'text-brand' : 'text-status-danger'

const DEVELOPER_MODE_STORAGE_KEY = 'jarvis.developer_mode'

const readStoredDeveloperMode = (): boolean =>
  import.meta.env.DEV || window.localStorage.getItem(DEVELOPER_MODE_STORAGE_KEY) === '1'

const isEditableShortcutTarget = (target: EventTarget | null): boolean => {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
}

const abbreviateModel = (model: string | null | undefined): { label: string; color: string } => {
  if (!model) return { label: '---', color: 'text-foreground-disabled' }
  const bare = model.includes('/') ? model.split('/').pop()! : model
  const label = bare.length > 18 ? `${bare.slice(0, 16)}…` : bare
  return { label, color: 'text-status-success' }
}

const formatMs = (ms: number | null | undefined): string => {
  if (ms == null) return '---'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
}

const formatTokens = (tokens: number | null | undefined): string => {
  if (tokens == null) return '---'
  return Math.abs(tokens) >= 1000 ? `${(tokens / 1000).toFixed(1)}k` : `${Math.round(tokens)}`
}

const contextBudgetTone = (used: number | null | undefined, budget: number | null | undefined): string => {
  if (used == null || budget == null || budget <= 0) return 'text-foreground-disabled'
  const ratio = used / budget
  return ratio < 0.75 ? 'text-status-success' : ratio < 0.9 ? 'text-brand' : 'text-status-danger'
}

const latestStage = (
  stages: Diagnostics['turn']['stages'] | undefined,
  key: string,
): NonNullable<Diagnostics['turn']['stages']>[number] | null => {
  return [...(stages ?? [])].reverse().find((stage) => stage.key === key) ?? null
}

const latestStageMs = (
  stages: Diagnostics['turn']['stages'] | undefined,
  key: string,
): number | null => {
  return latestStage(stages, key)?.ms ?? null
}

const sumLatestStageMs = (
  stages: Diagnostics['turn']['stages'] | undefined,
  keys: string[],
): number | null => {
  const values = keys
    .map((key) => latestStageMs(stages, key))
    .filter((value): value is number => value != null)
  return values.length ? values.reduce((sum, value) => sum + value, 0) : null
}

const llmStageHint = (stage: Diagnostics['turn']['active_stage'] | undefined | null): string | null => {
  if (!stage) return null
  const parts: string[] = []
  if (stage.status && stage.status !== 'ok') {
    const label = stage.status === 'retry_ok' ? 'retry ok' : stage.status.replace('_', ' ')
    parts.push(label)
  }
  if ((stage.retry_count ?? 0) > 0) {
    parts.push(`retry ${stage.retry_count}`)
  }
  if (stage.timeout_ms != null && ['waiting', 'retrying', 'timeout'].includes(stage.status ?? '')) {
    parts.push(`limit ${formatMs(stage.timeout_ms)}`)
  }
  return parts.length ? parts.join(' · ') : null
}

interface PerfStage {
  label: string
  detail: string
  value: number | null
  group?: 'pre_response' | 'post_first_audio'
  key?: string
  diagnostic?: boolean
}

type TurnStage = NonNullable<Diagnostics['turn']['stages']>[number]

const USER_FACING_STAGE_KEYS = new Set([
  'stt_batch',
  'stt_finalize_wait',
  'turn_detector',
  'llm',
  'code_exec',
  'tts_first_chunk',
])

const DIAGNOSTIC_STAGE_KEYS = new Set([
  'stt_stream_start',
  'stt_first_partial',
  'stt_stream_total',
  'tts_sentence',
  'turn_lock_wait',
  'db_history',
  'ctx_budget',
  'prompt_build',
  'tool_route',
  'tool_manifest',
])

const userFacingStageDetail = (stage: NonNullable<Diagnostics['turn']['stages']>[number]): string => {
  if (stage.key === 'stt_batch' || stage.key === 'stt_finalize_wait') return 'speech finalized'
  if (stage.key === 'turn_detector') return 'end-of-speech check'
  if (stage.key === 'llm') return ['first response token', llmStageHint(stage)].filter(Boolean).join(' · ')
  if (stage.key === 'code_exec') return 'tool execution'
  if (stage.key === 'tts_first_chunk') return 'first audio'
  return stage.detail
}

const diagnosticStageDetail = (stage: NonNullable<Diagnostics['turn']['stages']>[number]): string => {
  if (stage.key === 'stt_stream_total') return 'user speech stream duration'
  if (stage.key === 'stt_first_partial') return 'first partial transcript'
  if (stage.key === 'stt_stream_start') return 'STT connection setup'
  if (stage.key === 'tts_sentence') return 'follow-on sentence audio'
  if (stage.key === 'tool_route') return 'tool selection'
  if (stage.key === 'tool_manifest') return 'tool prompt assembly'
  if (stage.key === 'turn_lock_wait') return 'turn queue wait'
  if (stage.key === 'db_history') return 'history read'
  if (stage.key === 'ctx_budget') return 'context trimming'
  if (stage.key === 'prompt_build') return 'prompt assembly'
  return stage.detail
}

const sumStageMs = (
  stages: Diagnostics['turn']['stages'] | undefined,
  keys: string[],
): number | null => {
  const keySet = new Set(keys)
  const values = (stages ?? [])
    .filter((stage) => keySet.has(stage.key))
    .map((stage) => stage.ms)
    .filter((value): value is number => value != null)
  return values.length ? values.reduce((sum, value) => sum + value, 0) : null
}

const compactUserFacingStages = (stages: Diagnostics['turn']['stages'] | undefined): TurnStage[] => {
  const compacted = new Map<string, TurnStage>()
  for (const stage of stages ?? []) {
    if (!USER_FACING_STAGE_KEYS.has(stage.key)) continue
    const key = stage.iteration != null ? `${stage.key}:${stage.iteration}` : stage.key
    compacted.set(key, stage)
  }
  return Array.from(compacted.values())
}

// --- GLITCH TEXT COMPONENT ---
const GlitchLabel: React.FC<{ text: string }> = ({ text }) => {
  const [display, setDisplay] = useState(text);
  
  useEffect(() => {
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$#@%&*";
    let iterations = 0;
    
    const interval = setInterval(() => {
      setDisplay(() => 
        text.split("").map((_, index) => {
          if (index < iterations) return text[index];
          return chars[Math.floor(Math.random() * chars.length)];
        }).join("")
      );
      
      iterations += 1; // Speed of resolution
      
      if (iterations >= text.length) {
        setDisplay(text);
        clearInterval(interval);
      }
    }, 20); // Frame rate of glitch

    return () => clearInterval(interval);
  }, [text]);

  return (
    <div className="absolute -top-5 left-1/2 -translate-x-1/2 text-[10px] font-mono tracking-widest text-outline whitespace-nowrap z-50 pointer-events-none">
      {display}
    </div>
  );
};

// --- MENU COMPOSITIONS ---

const ShellNavButton: React.FC<{
  active?: boolean;
  label: string;
  icon: React.ReactNode;
  onClick: () => void;
  hasPopup?: boolean;
  expanded?: boolean;
}> = ({ active, label, icon, onClick, hasPopup = false, expanded }) => (
  <TacticalButton
    active={active}
    label={label}
    aria-label={label}
    aria-haspopup={hasPopup ? 'dialog' : undefined}
    aria-expanded={hasPopup ? Boolean(expanded) : undefined}
    onClick={onClick}
    className="min-h-10 w-auto px-2 py-0"
  >
    <span className="flex items-center gap-1.5">
      {icon}
      <span className="type-label-small whitespace-nowrap">
        {label}
      </span>
    </span>
  </TacticalButton>
);

// --- SNAPSHOT MENU ---

type SnapshotState = 'idle' | 'loading' | 'success' | 'error'

const SnapshotMenu: React.FC<{
  onClose: () => void;
}> = ({ onClose }) => {
  const [reason, setReason] = useState('')
  const [state, setState] = useState<SnapshotState>('idle')
  const [snapshotId, setSnapshotId] = useState<string | null>(null)

  const handleCapture = async () => {
    if (!reason.trim() || state === 'loading') return
    setState('loading')
    try {
      const resp = await authorizedFetch('/api/v1/snapshots/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: reason.trim() }),
      })
      if (!resp.ok) throw new Error(`${resp.status}`)
      const data = await resp.json() as { snapshot_id: string }
      setSnapshotId(data.snapshot_id)
      setState('success')
    } catch {
      setState('error')
    }
  }

  return (
    <StatusBarMenuContent onClose={onClose}>
      <MenuHeader>Diagnostic Snapshot</MenuHeader>
      {state === 'success' ? (
        <div className="px-2 py-3 text-center">
          <div className="text-[11px] font-mono text-status-success mb-1">CAPTURED</div>
          <div className="text-[9px] font-mono text-foreground-disabled tracking-wider">{snapshotId}</div>
        </div>
      ) : (
        <>
          <div className="px-2 pb-1">
            <textarea
              autoFocus
              placeholder="Describe what went wrong..."
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleCapture() }}
              rows={3}
              className="w-full rounded-control border border-outline bg-surface px-2 py-2 type-body text-foreground placeholder:text-foreground-disabled resize-none focus:outline-none focus:border-status-success/50 focus-visible:ring-2 focus-visible:ring-brand/70 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas transition-colors"
            />
          </div>
          <div className="px-2 pb-1.5">
            <button
              disabled={!reason.trim() || state === 'loading'}
              onClick={handleCapture}
              className="w-full rounded-control bg-status-success/10 py-2 type-label-small text-status-success transition-all duration-200 hover:bg-status-success/20 active:scale-[0.98] focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas disabled:cursor-not-allowed disabled:opacity-30"
            >
              {state === 'loading' ? 'Capturing…' : state === 'error' ? 'Failed — retry' : 'Capture'}
            </button>
          </div>
        </>
      )}
    </StatusBarMenuContent>
  )
}

const PerformanceMenu: React.FC<{
  diagnostics: Diagnostics | null;
  contextMetrics: ContextMetrics | null;
  systemLatency: number | null;
  onClose: () => void;
}> = ({ diagnostics, contextMetrics, systemLatency, onClose }) => {
  const turn = diagnostics?.turn
  const responseTotal = turn?.response_ms ?? turn?.total_ms ?? null
  const fullTurn = turn?.total_ms ?? null
  const tokensUsed = contextMetrics?.tokens_used ?? null
  const tokenBudget = contextMetrics?.budget ?? null
  const tokensLeft = tokenBudget != null && tokensUsed != null ? Math.max(0, tokenBudget - tokensUsed) : null
  const contextPercent = tokenBudget && tokensUsed != null
    ? Math.max(0, Math.min(100, (tokensUsed / tokenBudget) * 100))
    : null
  const historyLabel = contextMetrics
    ? `${contextMetrics.messages_kept} kept${contextMetrics.messages_dropped ? ` · ${contextMetrics.messages_dropped} dropped` : ''}`
    : '---'
  const rawStages = turn?.stages ?? []
  const modelStage = turn?.active_stage?.key === 'llm'
    ? turn.active_stage
    : latestStage(rawStages, 'llm')
  const modelHint = llmStageHint(modelStage)
  const userFacingStages: PerfStage[] = compactUserFacingStages(turn?.stages)
    .map((stage) => ({
      key: stage.key,
      label: stage.iteration != null ? `${stage.label} ${stage.iteration + 1}` : stage.label,
      detail: userFacingStageDetail(stage),
      value: stage.ms,
      group: stage.group,
    }))
  const diagnosticStages: PerfStage[] = (turn?.stages ?? [])
    .filter((stage) => DIAGNOSTIC_STAGE_KEYS.has(stage.key))
    .map((stage) => ({
      key: stage.key,
      label: stage.iteration != null ? `${stage.label} ${stage.iteration + 1}` : stage.label,
      detail: diagnosticStageDetail(stage),
      value: stage.ms,
      group: stage.group,
      diagnostic: true,
    }))
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0))
    .slice(0, 6)
  const mainDelay = userFacingStages
    .filter((stage) => stage.value != null)
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0))[0]
  const bottlenecks = userFacingStages
    .filter((stage) => stage.value != null)
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0))
    .slice(0, 5)
  const pipeline = [
    {
      label: 'Listen',
      detail: 'speech finalized',
      value: sumLatestStageMs(rawStages, ['stt_batch', 'stt_finalize_wait', 'turn_detector']),
    },
    {
      label: 'Prepare',
      detail: 'history + prompt',
      value: sumLatestStageMs(rawStages, ['db_history', 'ctx_budget', 'prompt_build']),
    },
    {
      label: 'Think',
      detail: ['first token', modelHint].filter(Boolean).join(' · '),
      value: modelStage?.ms ?? null,
    },
    {
      label: 'Act',
      detail: 'tools',
      value: sumStageMs(rawStages, ['code_exec']),
    },
    {
      label: 'Speak',
      detail: 'first audio',
      value: latestStageMs(rawStages, 'tts_first_chunk'),
    },
  ].filter((item) => item.value != null)

  return (
    <StatusBarMenuContent onClose={onClose}>
      <div className="max-h-[min(78vh,620px)] overflow-y-auto pr-1 [scrollbar-width:thin] [scrollbar-color:theme(colors.outline.DEFAULT)_transparent]">
        <MenuHeader>Performance</MenuHeader>

        <div className="px-2 py-2">
          <div className="rounded-panel bg-surface/40 px-3 py-3">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-foreground-disabled/70">
                  Time to Voice
                </div>
                <div className={cn("mt-1 text-2xl font-display tabular-nums", thresholdClass(responseTotal, 1500, 3000))}>
                  {formatMs(responseTotal)}
                </div>
                {fullTurn != null && responseTotal != null && fullTurn > responseTotal + 100 && (
                  <div className="mt-0.5 text-[9px] font-mono text-foreground-disabled">
                    total turn {formatMs(fullTurn)}
                  </div>
                )}
              </div>
              <div className="max-w-[140px] text-right">
                <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-foreground-disabled/70">
                  Main Delay
                </div>
                <div className="mt-1 truncate text-xs font-mono text-foreground">
                  {mainDelay ? mainDelay.label : '---'}
                </div>
                <div className="text-[10px] font-mono text-foreground-disabled">
                  {formatMs(mainDelay?.value)}
                </div>
              </div>
            </div>

            {!responseTotal && (
              <div className="mt-3 rounded-control bg-canvas/40 px-3 py-2 text-[10px] font-body tracking-wide text-foreground-disabled/80">
                Ask JARV1S something to capture the first turn.
              </div>
            )}
          </div>
        </div>

        {pipeline.length > 0 && (
          <>
            <Divider variant="simple" className="mx-2 my-1" />

            <div className="px-2 py-1.5">
              <div className="mb-2 text-[10px] font-mono uppercase tracking-[0.18em] text-foreground-disabled/60">
                Pipeline
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                {pipeline.map((item) => (
                  <div key={item.label} className="rounded-control bg-surface/35 px-2 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-[11px] font-body tracking-wide text-foreground-muted">{item.label}</span>
                      <span className={cn("text-[10px] font-mono tabular-nums", thresholdClass(item.value, 500, 1000))}>
                        {formatMs(item.value)}
                      </span>
                    </div>
                    <div className="mt-0.5 truncate text-[9px] font-mono text-foreground-disabled/70">
                      {item.detail}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}

        <Divider variant="simple" className="mx-2 my-1" />

        <div className="px-2 py-1.5">
          <div className="mb-2 text-[10px] font-mono uppercase tracking-[0.18em] text-foreground-disabled/60">
            Context
          </div>
          <div className="rounded-control bg-surface/35 px-3 py-2.5">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-[10px] font-mono uppercase tracking-[0.16em] text-foreground-disabled/65">
                  Tokens Left
                </div>
                <div className={cn("mt-1 text-lg font-display tabular-nums", contextBudgetTone(tokensUsed, tokenBudget))}>
                  {formatTokens(tokensLeft)}
                </div>
                <div className="mt-0.5 text-[9px] font-mono text-foreground-disabled/70">
                  {formatTokens(tokensUsed)} / {formatTokens(tokenBudget)} used
                </div>
              </div>
              <div className="max-w-[150px] text-right">
                <div className="text-[10px] font-mono uppercase tracking-[0.16em] text-foreground-disabled/65">
                  History
                </div>
                <div className="mt-1 text-xs font-mono text-foreground">
                  {historyLabel}
                </div>
                <div className="mt-0.5 text-[9px] font-mono text-foreground-disabled/70">
                  last context fit
                </div>
              </div>
            </div>
            {contextPercent != null && (
              <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-canvas/70">
                <div
                  className={cn(
                    "h-full rounded-full transition-all duration-500",
                    contextBudgetTone(tokensUsed, tokenBudget).replace('text-', 'bg-')
                  )}
                  style={{ width: `${Math.max(4, contextPercent)}%` }}
                />
              </div>
            )}
            {!contextMetrics && (
              <div className="mt-2 text-[9px] font-mono text-foreground-disabled/70">
                Ask JARV1S something to capture context metrics.
              </div>
            )}
          </div>
        </div>

        {bottlenecks.length > 0 && (
          <>
            <Divider variant="simple" className="mx-2 my-1" />

            <div className="px-2 py-1.5 space-y-2">
              <div className="text-[10px] font-mono uppercase tracking-[0.18em] text-foreground-disabled/60">
                Main Delays
              </div>
              {bottlenecks.map((stage, index) => {
                const percent = stage.value && responseTotal ? Math.max(4, Math.min(100, (stage.value / responseTotal) * 100)) : 0
                return (
                  <div key={`${stage.label}-${index}`}>
                    <div className="mb-1 flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-[11px] font-body tracking-wide text-foreground-muted">
                          {stage.label}
                        </div>
                        <div className="truncate text-[9px] font-mono text-foreground-disabled/70">
                          {stage.detail}
                        </div>
                      </div>
                      <span className={cn("shrink-0 text-[10px] font-mono tabular-nums", thresholdClass(stage.value, 500, 1000))}>
                        {formatMs(stage.value)}
                      </span>
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-surface">
                      <div
                        className={cn(
                          "h-full rounded-full transition-all duration-500",
                          thresholdClass(stage.value, 500, 1000).replace('text-', 'bg-')
                        )}
                        style={{ width: `${percent}%` }}
                      />
                    </div>
                  </div>
                )
              })}
            </div>
          </>
        )}

        {diagnosticStages.length > 0 && (
          <>
            <Divider variant="simple" className="mx-2 my-1" />

            <div className="px-2 py-1.5">
              <div className="mb-2 text-[10px] font-mono uppercase tracking-[0.18em] text-foreground-disabled/45">
                Diagnostics
              </div>
              <div className="space-y-1.5">
                {diagnosticStages.map((stage, index) => (
                  <div
                    key={`${stage.key}-${index}`}
                    className="flex items-center justify-between gap-3 rounded-control border border-outline/20 bg-surface/15 px-2 py-1.5"
                  >
                    <div className="min-w-0">
                      <div className="truncate text-[10px] font-body tracking-wide text-foreground-muted/75">
                        {stage.label}
                      </div>
                      <div className="truncate text-[9px] font-mono text-foreground-disabled/55">
                        {stage.detail}
                      </div>
                    </div>
                    <span className="shrink-0 text-[10px] font-mono tabular-nums text-foreground-disabled/80">
                      {formatMs(stage.value)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </>
        )}

        <Divider variant="simple" className="mx-2 my-1" />

        <div className="px-2 py-2 text-[10px] font-mono text-foreground-disabled tracking-wide">
          <div className="mb-2 uppercase tracking-[0.18em] text-foreground-disabled/60">Signals</div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
            <div className="flex justify-between gap-2">
              <span>Network</span>
              <span className={thresholdClass(systemLatency, 50, 150)}>{formatMs(systemLatency)}</span>
            </div>
            <div className="flex justify-between gap-2">
              <span>Backend</span>
              <span className={thresholdClass(diagnostics?.loop_lag_ms, 10, 50)}>{formatMs(diagnostics?.loop_lag_ms)}</span>
            </div>
            <div className="flex justify-between gap-2">
              <span>Model</span>
              <span className={abbreviateModel(turn?.model).color}>{abbreviateModel(turn?.model).label}</span>
            </div>
            <div className="flex justify-between gap-2">
              <span>Status</span>
              <span className="text-foreground uppercase">{turn?.status ?? '---'}</span>
            </div>
          </div>
        </div>
      </div>
    </StatusBarMenuContent>
  )
}

const AdminMenu: React.FC<{
  onClose: () => void;
  onPerformance: () => void;
  onSnapshot: () => void;
  onExport: () => void;
  exportStatus: string | null;
  canExport: boolean;
  onDisableDeveloperMode: () => void;
}> = ({ onClose, onPerformance, onSnapshot, onExport, exportStatus, canExport, onDisableDeveloperMode }) => (
  <StatusBarMenuContent onClose={onClose}>
    <MenuHeader>Diagnostics</MenuHeader>
    <div className="px-2 pb-1 type-meta leading-relaxed text-foreground-subtle">
      Developer tools for performance and support snapshots. Toggle with Cmd/Ctrl+Shift+D.
    </div>
    <MenuItem icon={<PulseIcon size={14} />} closeOnClick={false} onClick={onPerformance}>
      Performance diagnostics
    </MenuItem>
    <MenuItem icon={<BugIcon size={14} />} closeOnClick={false} onClick={onSnapshot}>
      Capture diagnostic snapshot
    </MenuItem>
    {canExport && (
      <>
        <MenuItem icon={<DownloadSimpleIcon size={14} />} closeOnClick={false} onClick={onExport}>
          Export metadata diagnostics
        </MenuItem>
        {exportStatus && (
          <div className="px-2 py-1 type-meta leading-relaxed text-foreground-subtle break-all">
            {exportStatus}
          </div>
        )}
        <div className="px-2 pb-2 type-meta leading-relaxed text-foreground-subtle">
          Support JSON only — dogfood save/restore: task desktop:data:backup
        </div>
      </>
    )}
    {!import.meta.env.DEV && (
      <>
        <Divider variant="simple" className="mx-2 my-1" />
        <MenuItem icon={<XCircleIcon size={14} />} onClick={onDisableDeveloperMode}>
          Turn off developer mode
        </MenuItem>
      </>
    )}
  </StatusBarMenuContent>
)

export const StatusBar: React.FC = () => {
  // 1. Subscribe to Store
  const connectionState = useJarvisStore((s) => s.connectionState);
  const hostState = useJarvisStore((s) => s.hostState);
  const reconnectAttempt = useJarvisStore((s) => s.reconnectAttempt);
  const agentState = useJarvisStore((s) => s.agentState);
  const systemLatency = useJarvisStore((s) => s.systemLatency);
  const coreName = useJarvisStore((s) => s.coreName);
  const isTranscriptVisible = useJarvisStore((s) => s.isTranscriptVisible);
  const transcriptData = useJarvisStore((s) => s.transcript);
  const partialTranscript = useJarvisStore((s) => s.partialTranscript);
  const hasTranscript = transcriptData.length > 0 || !!partialTranscript;
  const diagnostics = useJarvisStore((s) => s.diagnostics);
  const contextMetrics = useJarvisStore((s) => s.contextMetrics);
  const activeOverlay = useJarvisStore((s) => s.activeOverlay);
  const wakewordFeedbackVisible = useJarvisStore((s) => s.wakewordFeedbackVisible);
  const attentionState = useJarvisStore((s) => s.attentionState);
  const sessionState = useJarvisStore((s) => s.sessionState);
  const isMuted = useJarvisStore((s) => s.isMuted);
  const isSpeaking = useJarvisStore((s) => s.isSpeaking);
  const isAudioContextReady = useJarvisStore((s) => s.isAudioContextReady);
  const captureStalled = useJarvisStore((s) => s.audioDevices.captureStalled);
  
  // 2. Tooltip State
  const [activeLabel, setActiveLabel] = useState<string | null>(null);
  const [activeLabelSide, setActiveLabelSide] = useState<'left' | 'right' | null>(null);
  const [debouncedLabel, setDebouncedLabel] = useState<string | null>(null);

  // 3. Menu State
  const [activeMenu, setActiveMenu] = useState<string | null>(null);
  const [developerMode, setDeveloperMode] = useState(readStoredDeveloperMode);
  const [diagnosticsExportStatus, setDiagnosticsExportStatus] = useState<string | null>(null);

  const toggleMenu = (menu: string) => {
    setActiveMenu(prev => prev === menu ? null : menu);
  };

  useEffect(() => {
    if (activeOverlay) setActiveMenu(null);
  }, [activeOverlay]);

  const enableDeveloperMode = useCallback(() => {
    window.localStorage.setItem(DEVELOPER_MODE_STORAGE_KEY, '1');
    setDeveloperMode(true);
    useJarvisStore.getState().closeOverlay();
    setActiveMenu('admin');
  }, []);

  const disableDeveloperMode = useCallback(() => {
    window.localStorage.removeItem(DEVELOPER_MODE_STORAGE_KEY);
    setDeveloperMode(import.meta.env.DEV);
    setActiveMenu(null);
  }, []);

  const exportDesktopDiagnostics = useCallback(async () => {
    setDiagnosticsExportStatus('Exporting metadata…');
    try {
      const client = jarvisClient.diagnosticsSnapshot();
      const result = await exportDiagnosticsBundle(false, client);
      setDiagnosticsExportStatus(`Saved to ${result.path}`);
    } catch (error) {
      setDiagnosticsExportStatus(
        error instanceof Error ? error.message : 'Diagnostics export failed',
      );
    }
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!(event.shiftKey && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'd')) return;
      if (isEditableShortcutTarget(event.target)) return;
      event.preventDefault();
      if (!developerMode) {
        enableDeveloperMode();
        return;
      }
      useJarvisStore.getState().closeOverlay();
      setActiveMenu((current) => current === 'admin' ? null : 'admin');
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [developerMode, enableDeveloperMode]);

  useEffect(() => {
    // "After a while" logic (Debounce)
    const timer = setTimeout(() => {
      setDebouncedLabel(activeLabel);
    }, 50); // 50ms delay
    
    return () => clearTimeout(timer);
  }, [activeLabel]);

  // Status Configuration — trust pill answers “What is JARV1S doing?”
  const status = getSystemStatus(hostState, connectionState, agentState, reconnectAttempt);
  const dashboard = resolveDashboardStatus({
    hostState,
    connectionState,
    agentState,
    reconnectAttempt,
    isSpeaking,
    isAudioContextReady,
    isMuted,
    softMuted: sessionState.soft_muted,
    attentionMode: attentionState?.mode ?? 'active',
    captureStalled,
  })
  const statusLabel = dashboard.label
  const statusHoverLabel = dashboard.hoverLabel
  const statusColor = dashboard.color
  const statusHologramColor = dashboard.hologramColor
  const statusPulse = dashboard.pulse

  const closeRightSurface = useCallback(() => {
    setActiveMenu(null)
    useJarvisStore.getState().closeOverlay()
  }, [])

  // Workspaces (activeOverlay) win over glance menus so opening an overlay from a
  // menu morphs the host in place instead of dismissing then reopening.
  let rightSurface: StatusBarSurface | null = null
  if (activeOverlay === 'operations') {
    rightSurface = {
      id: 'operations',
      kind: 'workspace',
      size: 'workspace',
      label: 'Activity',
      color: status.hologramColor,
      children: <OperationsPanelContent />,
    }
  } else if (activeOverlay === 'presence') {
    rightSurface = {
      id: 'presence',
      kind: 'workspace',
      size: 'workspace-narrow',
      label: 'Devices',
      color: status.hologramColor,
      children: <PresencePanelContent />,
    }
  } else if (activeOverlay === 'smart_home') {
    rightSurface = {
      id: 'home',
      kind: 'workspace',
      size: 'workspace-narrow',
      label: 'Home',
      color: status.hologramColor,
      children: <HomePanelContent />,
    }
  } else if (activeOverlay === 'home_assistant') {
    rightSurface = {
      id: 'home-assistant',
      kind: 'workspace',
      size: 'workspace-narrow',
      label: 'Home Assistant',
      color: status.hologramColor,
      children: <HomeAssistantPanelContent />,
    }
  } else if (activeOverlay === 'integrations') {
    rightSurface = {
      id: 'apps',
      kind: 'workspace',
      size: 'workspace',
      label: 'Apps',
      color: status.hologramColor,
      children: <IntegrationsPanelContent />,
    }
  } else if (activeOverlay === 'settings') {
    rightSurface = {
      id: 'settings',
      kind: 'workspace',
      size: 'workspace',
      label: 'Settings',
      color: status.hologramColor,
      children: <SettingsPanelContent />,
    }
  } else if (activeMenu === 'admin') {
    rightSurface = {
      id: 'diagnostics',
      kind: 'menu',
      size: 'standard',
      label: 'Diagnostics',
      role: 'menu',
      color: status.hologramColor,
      children: (
        <AdminMenu
          onClose={closeRightSurface}
          onPerformance={() => setActiveMenu('performance')}
          onSnapshot={() => setActiveMenu('snapshot')}
          onExport={() => void exportDesktopDiagnostics()}
          exportStatus={diagnosticsExportStatus}
          canExport={isDesktopApp()}
          onDisableDeveloperMode={disableDeveloperMode}
        />
      ),
    }
  } else if (activeMenu === 'performance') {
    rightSurface = {
      id: 'performance',
      kind: 'menu',
      size: 'wide',
      label: 'Performance diagnostics',
      role: 'region',
      color: status.hologramColor,
      children: (
        <PerformanceMenu
          diagnostics={diagnostics}
          contextMetrics={contextMetrics}
          systemLatency={systemLatency}
          onClose={closeRightSurface}
        />
      ),
    }
  } else if (activeMenu === 'snapshot') {
    rightSurface = {
      id: 'snapshot',
      kind: 'menu',
      size: 'compact',
      label: 'Diagnostic snapshot',
      role: 'region',
      color: status.hologramColor,
      children: <SnapshotMenu onClose={closeRightSurface} />,
    }
  } else if (activeMenu === 'activity') {
    rightSurface = {
      id: 'activity',
      kind: 'menu',
      size: 'expanded',
      label: 'Recent activity',
      role: 'region',
      color: status.hologramColor,
      children: <RecentActivityPopover onClose={closeRightSurface} />,
    }
  }

  return (
    <div className="w-full z-50 relative select-none pointer-events-none">
      {/* Background Gradient Layer - Extends beyond the items for a soft fade */}
      <div className="absolute inset-x-0 top-0 h-safe-top bg-gradient-to-b from-canvas via-canvas/98 to-transparent" />
      
      {/* Content Layer - Precisely contains the items */}
      <div className="relative flex items-start justify-between px-6 pt-status-bar-inset">
        {/* LEFT CLUSTER: Status Pill + Contextual Actions */}
        <div className="flex items-center gap-3">
          {/* LEFT PILL: Trust Signal + Status */}
          <div className="relative pointer-events-auto">
            <Hologram
              role="button"
              tabIndex={0}
              aria-label={`JARV1S status: ${statusHoverLabel ?? statusLabel}`}
              aria-haspopup="dialog"
              aria-expanded={activeMenu === 'trust'}
              variant="ringed"
              color={statusHologramColor}
              onMouseEnter={() => {
                  setActiveLabel(statusHoverLabel);
                  setActiveLabelSide(statusHoverLabel ? 'left' : null);
              }}
              onMouseLeave={() => {
                  setActiveLabel(null);
                  setActiveLabelSide(null);
              }}
              onClick={() => {
                useJarvisStore.getState().closeOverlay();
                toggleMenu('trust');
              }}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  useJarvisStore.getState().closeOverlay();
                  toggleMenu('trust');
                }
              }}
              className={cn(
                "relative h-11 flex items-center gap-3 px-4 rounded-full overflow-visible cursor-pointer opacity-80 hover:opacity-100",
                "transition-opacity duration-feedback",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas",
                activeMenu === 'trust' && "text-foreground opacity-100"
              )}
            >
              {/* Status Label (Floating above) */}
              {debouncedLabel && activeLabelSide === 'left' && !activeMenu && (
                  <GlitchLabel text={debouncedLabel} />
              )}

              <div
                  className={cn(
                      "w-2.5 h-2.5 rounded-full transition-all duration-300 flex-shrink-0",
                      statusColor,
                      statusPulse && "animate-pulse",
                      dashboard.iconClass
                  )}
                  aria-hidden
              />
              <div className="w-40 type-label text-foreground-muted whitespace-nowrap truncate text-right transition-all duration-300">
                  {statusLabel}
              </div>
            </Hologram>

            {activeMenu === 'trust' && (
              <FloatingStatusBarMenu align="left" hologramColor={status.hologramColor} onClose={() => setActiveMenu(null)}>
                <MenuHeader>Status</MenuHeader>
                <MenuInfo
                  label="State"
                  icon={<WifiHighIcon size={14} />}
                  statusIndicator={connectionState === 'connected' ? 'success' : connectionState === 'connecting' || connectionState === 'reconnecting' ? 'warning' : 'error'}
                  value={status.label}
                />
                <MenuInfo
                  label="Latency"
                  icon={<PulseIcon size={14} />}
                  statusIndicator={
                    !systemLatency ? 'neutral' :
                    systemLatency < 50 ? 'success' :
                    systemLatency < 150 ? 'warning' : 'error'
                  }
                  value={
                    <span className={thresholdClass(systemLatency, 50, 150)}>
                      {formatMs(systemLatency)}
                    </span>
                  }
                />
                {developerMode && (
                  <>
                    <MenuInfo label="Core" icon={<HouseIcon size={14} />} value={coreName} />
                    <MenuInfo
                      label="Context"
                      icon={<PulseIcon size={14} />}
                      statusIndicator={
                        !contextMetrics ? 'neutral' :
                        contextMetrics.budget > 0 && contextMetrics.tokens_used / contextMetrics.budget >= 0.9 ? 'error' :
                        contextMetrics.budget > 0 && contextMetrics.tokens_used / contextMetrics.budget >= 0.75 ? 'warning' :
                        'success'
                      }
                      value={
                        <span className={contextBudgetTone(contextMetrics?.tokens_used, contextMetrics?.budget)}>
                          {formatTokens(
                            contextMetrics
                              ? Math.max(0, contextMetrics.budget - contextMetrics.tokens_used)
                              : null
                          )} left
                        </span>
                      }
                    />
                    <MenuInfo
                      label="History"
                      icon={<ChatTextIcon size={14} />}
                      value={
                        contextMetrics
                          ? `${contextMetrics.messages_kept} kept${contextMetrics.messages_dropped ? ` · ${contextMetrics.messages_dropped} cut` : ''}`
                          : '---'
                      }
                    />
                    <Divider variant="simple" className="mx-2 my-1" />
                    <MenuItem
                      icon={<ArrowsClockwiseIcon size={14} />}
                      onClick={() => {
                        jarvisClient.disconnect();
                        setTimeout(() => jarvisClient.connect(), 500);
                        setActiveMenu(null);
                      }}
                    >
                      Reconnect
                    </MenuItem>
                  </>
                )}
              </FloatingStatusBarMenu>
            )}
          </div>

          {/* CONTEXTUAL TOGGLE: Transcript visibility */}
          {hasTranscript && (
            <TacticalButton 
              active={isTranscriptVisible}
              onClick={() => useJarvisStore.getState().toggleTranscript()}
              className="w-11 h-11 pointer-events-auto"
            >
              <ChatTextIcon size={20} weight={isTranscriptVisible ? 'fill' : 'regular'} />
            </TacticalButton>
          )}

          {/* WAKE WORD FEEDBACK: NOT ME button — visible for 5s after detection + response */}
          {wakewordFeedbackVisible && (
            <TacticalButton
              onClick={() => {
                jarvisClient.sendWakewordFeedback('false_positive')
                jarvisClient.sendMessage('system.stop', {})
              }}
              className="w-auto px-3 h-11 pointer-events-auto"
            >
              <div className="flex items-center gap-1.5">
                <XCircleIcon size={14} className="text-status-danger" />
                <span className="text-[10px] font-mono tracking-widest opacity-80">NOT ME</span>
              </div>
            </TacticalButton>
          )}
        </div>

        {/* RIGHT PILL: System Cluster — shared geometry anchors attached shell surfaces */}
        <Hologram
          role="navigation"
          aria-label="JARV1S navigation"
          variant="ringed"
          color={status.hologramColor}
          className="relative z-50 flex h-status-nav items-center overflow-visible rounded-full p-[3px] text-foreground-muted opacity-80 pointer-events-auto"
        >
          {/* Label Display (Floating above) */}
          {debouncedLabel && activeLabelSide === 'right' && <GlitchLabel text={debouncedLabel} />}

          {/* p-[3px] matches shadow-hologram-inset inner ring; height comes from min-h-10 buttons. */}
          <div className="relative z-50 flex min-w-0 items-center gap-1">
            <ShellNavButton
              active={
                activeOverlay === 'smart_home' ||
                activeOverlay === 'home_assistant' ||
                activeOverlay === 'presence'
              }
              label="Home"
              hasPopup
              expanded={
                activeOverlay === 'smart_home' ||
                activeOverlay === 'home_assistant' ||
                activeOverlay === 'presence'
              }
              icon={<HouseIcon size={16} />}
              onClick={() => {
                setActiveMenu(null);
                if (activeOverlay === 'smart_home') {
                  useJarvisStore.getState().closeOverlay();
                } else {
                  useJarvisStore.getState().openOverlay('smart_home');
                }
              }}
            />

            <ShellNavButton
              active={activeMenu === 'activity' || activeOverlay === 'operations'}
              label="Activity"
              hasPopup
              expanded={activeMenu === 'activity' || activeOverlay === 'operations'}
              icon={<ClockCounterClockwiseIcon size={16} />}
              onClick={() => {
                if (activeOverlay === 'operations') {
                  useJarvisStore.getState().closeOverlay();
                  return;
                }
                useJarvisStore.getState().closeOverlay();
                toggleMenu('activity');
              }}
            />

            <ShellNavButton
              active={activeOverlay === 'integrations'}
              label="Apps"
              hasPopup
              expanded={activeOverlay === 'integrations'}
              icon={<PuzzlePieceIcon size={16} />}
              onClick={() => {
                setActiveMenu(null);
                if (activeOverlay === 'integrations') {
                  useJarvisStore.getState().closeOverlay();
                } else {
                  useJarvisStore.getState().openOverlay('integrations');
                }
              }}
            />

            <div className="mx-0.5 w-px h-3 flex flex-col" aria-hidden>
              <div className="h-[3px] bg-outline" />
              <div className="flex-1 bg-surface" />
            </div>

            <ShellNavButton
              active={activeOverlay === 'settings'}
              label="Settings"
              hasPopup
              expanded={activeOverlay === 'settings'}
              icon={<GearIcon size={16} />}
              onClick={() => {
                setActiveMenu(null);
                if (activeOverlay === 'settings') {
                  useJarvisStore.getState().closeOverlay();
                } else {
                  useJarvisStore.getState().openOverlay('settings');
                }
              }}
            />

            {developerMode && (
              <ShellNavButton
                active={activeMenu === 'admin' || activeMenu === 'performance' || activeMenu === 'snapshot'}
                label="Diagnostics"
                hasPopup
                expanded={activeMenu === 'admin' || activeMenu === 'performance' || activeMenu === 'snapshot'}
                icon={<BugIcon size={16} />}
                onClick={() => {
                  useJarvisStore.getState().closeOverlay();
                  toggleMenu('admin');
                }}
              />
            )}
          </div>

        </Hologram>
        <StatusBarSurfaceHost surface={rightSurface} onClose={closeRightSurface} />
      </div>
    </div>
  );
};
