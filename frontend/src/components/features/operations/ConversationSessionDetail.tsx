import React, { useEffect, useMemo, useState } from 'react'
import { ArrowLeftIcon, CopyIcon } from '@phosphor-icons/react'
import ReactMarkdown from 'react-markdown'
import { operationsApi } from '../../../client/operationsApi'
import { cn } from '../../../utils/cn'
import { Button } from '../../ui/Button'
import { Disclosure } from '../../ui/Disclosure'
import { DataField } from '../../ui/PanelSection'
import { Placeholder } from '../../ui/Placeholder'
import { StatusPill } from '../../ui/StatusPill'
import {
  formatModality,
  formatSessionWhen,
  truncateTitle,
} from './conversationMeta'
import type { ConversationSession } from './ConversationTimeline'
import type { OperationRunDetail, OperationTraceLine } from '../../../types/operations'

interface ConversationMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: string
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString(undefined, {
    hour: 'numeric',
    minute: '2-digit',
  })
}

function visibleMessage(line: OperationTraceLine): boolean {
  if (line.role === 'user') return !line.turn_type
  return line.role === 'assistant' && (!line.turn_type || line.turn_type === 'text_only')
}

function dialogueFor(
  session: ConversationSession,
  details: OperationRunDetail[],
): ConversationMessage[] {
  const detailById = new Map(details.map((detail) => [detail.id, detail]))
  const messages: ConversationMessage[] = []

  session.turns
    .slice()
    .reverse()
    .forEach((turn) => {
      const detail = detailById.get(turn.turn_id ?? turn.detail_ref.id)
      const lines = detail?.attempts.flatMap((attempt) => attempt.trace) ?? []
      const visible = lines.filter(visibleMessage)
      const hasUserMessage = visible.some((line) => line.role === 'user')

      if (!hasUserMessage) {
        messages.push({
          id: `${turn.activity_id}:user`,
          role: 'user',
          content: turn.title,
          timestamp: turn.occurred_at,
        })
      }

      visible.forEach((line, index) => {
        const content = line.content.trim()
        if (!content) return
        messages.push({
          id: `${turn.activity_id}:${line.role}:${index}`,
          role: line.role as 'user' | 'assistant',
          content,
          timestamp: line.timestamp,
        })
      })
    })

  return messages.sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp))
}

function Diagnostics({ details }: { details: OperationRunDetail[] }) {
  return (
    <Disclosure
        label="Technical details"
        className="border-t border-outline/20 pt-4"
        summaryClassName="text-foreground-subtle"
        contentClassName="space-y-2 pb-1 pt-2"
        trailing={(
          <span className="font-mono text-[12px] text-foreground-subtle">
            {details.length} {details.length === 1 ? 'exchange' : 'exchanges'}
          </span>
        )}
      >
        {details.map((detail, index) => {
          const attempt = detail.attempts[0]
          const perf = attempt?.perf
          const toolCalls = attempt?.trace.filter((line) => line.turn_type === 'tool_call').length ?? 0
          return (
            <section key={detail.id} className="rounded-control bg-surface/10 px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <h5 className="type-label text-foreground-muted">Exchange {index + 1}</h5>
                <span className="type-meta tabular-nums text-foreground-subtle">
                  {perf?.total_ms != null ? `${Math.round(perf.total_ms)} ms` : detail.status}
                </span>
              </div>
              <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-3 border-t border-outline/15 pt-3 sm:grid-cols-4">
                <DataField label="Status" value={detail.status} />
                <DataField label="Input" value={formatModality(detail.modality) ?? '—'} />
                <DataField label="Model" value={perf?.model ?? '—'} />
                <DataField label="Tools" value={String(toolCalls)} />
              </dl>
              {attempt?.trace.length ? (
                <Disclosure
                    label={`Trace · ${attempt.trace.length} ${attempt.trace.length === 1 ? 'event' : 'events'}`}
                    className="mt-3 border-t border-outline/15 pt-1"
                    summaryClassName="text-foreground-subtle"
                    contentClassName="space-y-3 pb-3"
                  >
                    {attempt.trace.map((line, lineIndex) => (
                      <div key={`${detail.id}:${lineIndex}`} className="text-[12px] font-mono leading-relaxed">
                        <div className="uppercase tracking-[0.08em] text-foreground-subtle">
                          {line.role}{line.turn_type ? ` · ${line.turn_type.replace(/_/g, ' ')}` : ''}
                        </div>
                        <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-[12px] text-foreground-muted">
                          {line.code ?? line.output ?? line.content}
                        </pre>
                      </div>
                    ))}
                </Disclosure>
              ) : null}
            </section>
          )
        })}
    </Disclosure>
  )
}

export const ConversationSessionDetail: React.FC<{
  session: ConversationSession
  onBack: () => void
}> = ({ session, onBack }) => {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [details, setDetails] = useState<OperationRunDetail[]>([])
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    let cancelled = false
    setState('loading')
    setCopied(false)
    Promise.all(
      session.turns
        .slice()
        .reverse()
        .map((turn) => operationsApi.userTurnDetail(turn.turn_id ?? turn.detail_ref.id)),
    )
      .then((loaded) => {
        if (!cancelled) {
          setDetails(loaded)
          setState('ready')
        }
      })
      .catch(() => {
        if (!cancelled) {
          setDetails([])
          setState('error')
        }
      })
    return () => {
      cancelled = true
    }
  }, [session])

  const messages = useMemo(() => dialogueFor(session, details), [details, session])
  const modalities = Array.from(
    new Set(details.map((detail) => formatModality(detail.modality)).filter(Boolean)),
  ) as string[]
  const needsAttention = session.outcome !== 'succeeded'
  const showDiagnostics = import.meta.env.DEV
    || window.localStorage.getItem('jarvis.developer_mode') === '1'
  const exchangeLabel = `${session.turns.length} ${session.turns.length === 1 ? 'exchange' : 'exchanges'}`
  const metaParts = [
    exchangeLabel,
    ...modalities,
    session.sourceLabel,
    formatSessionWhen(session.startedAt, session.endedAt),
  ]

  const copyTranscript = async () => {
    const transcript = messages
      .map((message) => `${message.role === 'user' ? 'You' : 'JARV1S'}: ${message.content}`)
      .join('\n\n')
    await navigator.clipboard.writeText(transcript)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="p-6">
      <Button
        variant="ghost"
        color="action"
        size="sm"
        className="mb-3 h-10 lg:hidden"
        onClick={onBack}
        icon={<ArrowLeftIcon size={14} />}
      >
        Back to conversations
      </Button>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="type-label-small text-foreground-subtle">
            Conversation
          </div>
          <h3 className="mt-1 type-section text-foreground">
            {truncateTitle(session.title, 88)}
          </h3>
          <p className="mt-2 type-meta tabular-nums text-foreground-subtle">
            {metaParts.join(' · ')}
          </p>
        </div>
        {needsAttention && (
          <StatusPill
            tone={session.outcome === 'failed' ? 'error' : 'warning'}
            className="normal-case tracking-[0.04em]"
          >
            {session.outcome === 'failed' ? 'Needs attention' : session.outcome}
          </StatusPill>
        )}
      </div>

      {state === 'loading' && <Placeholder className="mt-4">Loading conversation…</Placeholder>}
      {state === 'error' && <Placeholder className="mt-4" tone="error">Could not load conversation.</Placeholder>}
      {state === 'ready' && (
        <>
          <div className="mt-5 flex items-center justify-between gap-3 border-t border-outline/20 pt-4">
            <h4 className="type-heading text-foreground">Transcript</h4>
            <Button
              variant="ghost"
              color="subtle"
              size="sm"
              className="h-10"
              onClick={() => void copyTranscript()}
              icon={<CopyIcon size={14} />}
            >
              {copied ? 'Copied' : 'Copy'}
            </Button>
          </div>
          <div className="mt-3 space-y-0" aria-label="Conversation transcript">
            {messages.map((message) => (
              <article
                key={message.id}
                className={cn(
                  'border-l px-4 py-3',
                  message.role === 'assistant'
                    ? 'border-brand/45 bg-brand/[0.03]'
                    : 'border-outline/35',
                )}
              >
                <div className="flex items-center justify-between gap-3">
                  <span className={cn(
                    'font-mono text-[12px] uppercase tracking-[0.12em]',
                    message.role === 'assistant' ? 'text-brand/80' : 'text-foreground-subtle',
                  )}>
                    {message.role === 'user' ? 'You' : 'JARV1S'}
                  </span>
                  <time className="font-mono text-[12px] text-foreground-subtle" dateTime={message.timestamp}>
                    {formatTime(message.timestamp)}
                  </time>
                </div>
                {message.role === 'assistant' ? (
                  <div className="mt-2 space-y-2 font-body text-[14px] leading-relaxed text-foreground-muted [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-0 [&_strong]:font-semibold [&_strong]:text-foreground [&_ul]:list-disc [&_ul]:pl-5">
                    <ReactMarkdown>{message.content}</ReactMarkdown>
                  </div>
                ) : (
                  <p className="mt-2 whitespace-pre-wrap font-body text-[14px] leading-relaxed text-foreground">
                    {message.content}
                  </p>
                )}
              </article>
            ))}
          </div>
          {showDiagnostics && (
            <div className="mt-6">
              <Diagnostics details={details} />
            </div>
          )}
        </>
      )}
    </div>
  )
}
