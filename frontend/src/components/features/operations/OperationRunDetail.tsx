import React, { useEffect, useState } from 'react'
import { operationsApi } from '../../../client/operationsApi'
import { Placeholder } from '../../ui/Placeholder'
import type { ActivityDetailRef, OperationRunDetail as OperationRunDetailData } from '../../../types/operations'

function formatKvBlock(title: string, data: Record<string, unknown> | null | undefined) {
  if (!data || Object.keys(data).length === 0) return null
  return (
    <div className="rounded-control bg-surface/30 px-3 py-2 text-[12px] font-mono text-foreground-subtle">
      <div className="mb-1 uppercase tracking-[0.12em] text-foreground-muted/80">{title}</div>
      <div className="space-y-1">
        {Object.entries(data).map(([key, value]) => (
          <div key={key} className="flex gap-2">
            <span className="shrink-0 text-foreground-disabled">{key}</span>
            <span className="min-w-0 break-all text-foreground/70">{String(value)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export const OperationRunDetail: React.FC<{
  detailRef: ActivityDetailRef
}> = ({ detailRef }) => {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [detail, setDetail] = useState<OperationRunDetailData | null>(null)

  useEffect(() => {
    let cancelled = false
    setState('loading')
    const loader = detailRef.kind === 'turn'
      ? operationsApi.userTurnDetail(detailRef.id)
      : detailRef.kind === 'trigger_instance'
        ? operationsApi.runDetail(detailRef.id)
        : Promise.reject(new Error('unsupported detail kind'))

    loader
      .then((data) => {
        if (!cancelled) {
          setDetail(data)
          setState('ready')
        }
      })
      .catch(() => {
        if (!cancelled) setState('error')
      })
    return () => {
      cancelled = true
    }
  }, [detailRef.id, detailRef.kind])

  if (state === 'loading') {
    return <Placeholder className="mt-2">Loading run detail…</Placeholder>
  }
  if (state === 'error' || !detail) {
    return <Placeholder className="mt-2" tone="error">Could not load run detail.</Placeholder>
  }

  const nodeLabel = detail.node_label || detail.node_id

  return (
    <div className="mt-3 space-y-3 rounded-control bg-canvas/45 p-3">
      {detailRef.kind === 'turn' && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] font-mono text-foreground-subtle">
          {nodeLabel ? <span>Node {nodeLabel}</span> : null}
          {detail.modality ? <span>Modality {detail.modality}</span> : null}
          <span>Status {detail.status}</span>
        </div>
      )}
      {detail.attempts.length === 0 ? (
        <div className="text-[12px] font-mono text-foreground-subtle">No turn attempts recorded for this run.</div>
      ) : detail.attempts.map((attempt) => (
        <div key={attempt.turn_id} className="space-y-2">
          <div className="flex items-center justify-between text-[12px] font-mono uppercase tracking-[0.12em] text-foreground-subtle">
            <span>{attempt.turn_id}</span>
            {attempt.perf?.total_ms != null ? <span>{Math.round(attempt.perf.total_ms)}ms</span> : null}
          </div>
          {attempt.perf && (
            <>
              <div className="grid grid-cols-3 gap-2 rounded-control bg-surface/30 px-3 py-2 text-[12px] font-mono text-foreground-subtle">
                <span>Outcome {attempt.perf.status ?? 'unknown'}</span>
                <span>First {attempt.perf.response_ms != null ? `${Math.round(attempt.perf.response_ms)}ms` : 'n/a'}</span>
                <span>Total {attempt.perf.total_ms != null ? `${Math.round(attempt.perf.total_ms)}ms` : 'n/a'}</span>
              </div>
              {formatKvBlock('STT', attempt.perf.stt)}
              {formatKvBlock('Endpointing', attempt.perf.turn_detection)}
              {formatKvBlock('Voice', attempt.perf.voice)}
              {formatKvBlock('Tool routing', attempt.perf.tool_routing)}
            </>
          )}
          {attempt.protocols.map((protocol) => (
            <div key={`${attempt.turn_id}:${protocol.protocol_name}:${protocol.started_at ?? ''}`} className="rounded-control bg-surface/30 px-3 py-2 text-[12px] font-mono text-foreground-subtle">
              Protocol {protocol.protocol_name} · {protocol.status}
            </div>
          ))}
          <div className="space-y-2">
            {attempt.trace.map((line, index) => (
              <div key={`${attempt.turn_id}:${index}`} className="border-t border-outline/10 pt-2 text-[12px] font-mono leading-relaxed text-foreground-subtle first:border-0 first:pt-0">
                <span className="text-foreground-muted/80 uppercase">{line.role}</span>
                {line.turn_type ? <span className="opacity-50"> · {line.turn_type}</span> : null}
                <div className="whitespace-pre-wrap break-words text-foreground/70">
                  {line.turn_type === 'tool_call' && line.code
                    ? line.code.slice(0, 800)
                    : line.turn_type === 'tool_result' && line.output
                      ? line.output.slice(0, 800)
                      : (line.content || '').slice(0, 800)}
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
