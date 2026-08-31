import React, { useEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import { CaretRightIcon } from '@phosphor-icons/react'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { cn } from '../../../utils/cn'
import { startsNewConversation } from '../../../utils/conversation'
import { TranscriptItem as TranscriptItemType } from '../../../types'
import { CapabilityReceiptItem } from './CapabilityReceiptItem'
import {
  formatTranscriptTime,
  groupTranscriptTurns,
  isUnsolicitedTurn,
  turnTimestamp,
  type TranscriptTurn,
} from './transcriptTurns'

const TurnHeader: React.FC<{
  speaker: 'user' | 'assistant'
  timestamp: number
  notice?: boolean
}> = ({ speaker, timestamp, notice = false }) => (
  <div className="flex items-baseline gap-2">
    <span
      className={cn(
        'type-label-small',
        notice
          ? 'text-foreground-subtle'
          : speaker === 'assistant'
            ? 'text-brand-output'
            : 'text-foreground-subtle',
      )}
    >
      {speaker === 'user' ? 'You' : 'Jarvis'}
    </span>
    {notice && (
      <span className="type-meta text-foreground-subtle">Notice</span>
    )}
    <span className="type-meta tabular-nums text-foreground-subtle">
      {formatTranscriptTime(timestamp)}
    </span>
  </div>
)

const TextBody: React.FC<{ item: TranscriptItemType; quiet?: boolean }> = ({ item, quiet = false }) => {
  const isUser = item.sender === 'user'
  const contentClassName = cn(
    'type-body',
    item.isPartial ? 'text-brand' : quiet ? 'text-foreground-muted' : 'text-foreground',
  )

  return (
    <div className="flex flex-col gap-2">
      {isUser ? (
        <p className={cn(contentClassName, 'whitespace-pre-wrap')}>{item.text}</p>
      ) : (
        <div
          className={cn(
            contentClassName,
            'space-y-2 [&_em]:italic [&_li]:my-1 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-0 [&_strong]:font-semibold [&_ul]:list-disc [&_ul]:pl-5',
            quiet ? '[&_strong]:text-foreground-muted' : '[&_strong]:text-foreground',
          )}
        >
          <ReactMarkdown>{item.text ?? ''}</ReactMarkdown>
        </div>
      )}
      {item.attachments && item.attachments.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {item.attachments.map((att, i) => (
            <img
              key={i}
              src={att.url}
              alt="Attachment"
              className="max-h-[150px] max-w-[200px] rounded-control border border-outline object-cover"
            />
          ))}
        </div>
      )}
    </div>
  )
}

const NoticeTurn: React.FC<{ item: TranscriptItemType }> = ({ item }) => (
  <p className="pr-4 type-meta text-foreground-subtle">
    {item.text ?? "Jarvis didn't reply."}
  </p>
)

const ConversationBoundary: React.FC = () => (
  <div className="mt-8 mb-6 flex items-center gap-3 pr-4" role="separator">
    <span className="h-px flex-1 bg-outline/50" />
    <span className="type-meta text-foreground-subtle">New conversation after 2 hours idle</span>
    <span className="h-px flex-1 bg-outline/50" />
  </div>
)

const ReasoningPart: React.FC<{ item: TranscriptItemType }> = ({ item }) => {
  const toggleCollapse = useJarvisStore((state) => state.toggleTranscriptItemCollapse)
  const collapsed = item.isCollapsed ?? true

  return (
    <div>
      <button
        type="button"
        onClick={() => toggleCollapse(item.id)}
        aria-expanded={!collapsed}
        className={cn(
          'flex min-h-10 items-center gap-2 type-label-small text-foreground-subtle',
          'transition-colors duration-feedback hover:text-foreground',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/60 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas',
        )}
      >
        <CaretRightIcon
          size={12}
          weight="bold"
          className={cn(
            'shrink-0 transition-transform duration-feedback ease-hologram motion-reduce:transition-none',
            !collapsed && 'rotate-90',
          )}
          aria-hidden
        />
        Reasoning
      </button>
      {!collapsed && (
        <p className="mt-1 whitespace-pre-wrap rounded-control border border-outline/50 bg-surface/20 px-3 py-2 type-body text-foreground-muted">
          {item.text}
        </p>
      )}
    </div>
  )
}

const SpokenTurn: React.FC<{
  speaker: 'user' | 'assistant'
  timestamp: number
  notice?: boolean
  children: React.ReactNode
}> = ({ speaker, timestamp, notice = false, children }) => (
  <article
    className={cn(
      'flex flex-col gap-1 pr-4',
      notice && 'border-l border-outline/40 pl-3',
    )}
    aria-label={`${speaker === 'user' ? 'You' : notice ? 'Jarvis notice' : 'Jarvis'}, ${formatTranscriptTime(timestamp)}`}
  >
    <TurnHeader speaker={speaker} timestamp={timestamp} notice={notice} />
    <div className="flex flex-col gap-2">{children}</div>
  </article>
)

const AssistantParts: React.FC<{ items: TranscriptItemType[]; quiet?: boolean }> = ({
  items,
  quiet = false,
}) => (
  <>
    {items.map((item) => {
      if (item.type === 'code') {
        return <CapabilityReceiptItem key={item.id} item={item} />
      }
      if (item.type === 'reasoning') {
        return <ReasoningPart key={item.id} item={item} />
      }
      if (!item.text?.trim() && !item.attachments?.length) return null
      return <TextBody key={item.id} item={item} quiet={quiet} />
    })}
  </>
)

function renderTurn(turn: TranscriptTurn, notice = false) {
  if (turn.kind === 'notice') {
    return <NoticeTurn item={turn.item} />
  }
  if (turn.kind === 'user') {
    return (
      <SpokenTurn speaker="user" timestamp={turn.item.timestamp}>
        <TextBody item={turn.item} />
      </SpokenTurn>
    )
  }
  return (
    <SpokenTurn speaker="assistant" timestamp={turnTimestamp(turn)} notice={notice}>
      <AssistantParts items={turn.items} quiet={notice} />
    </SpokenTurn>
  )
}

export const TranscriptWidget: React.FC = () => {
  const transcript = useJarvisStore((state) => state.transcript)
  const partialTranscript = useJarvisStore((state) => state.partialTranscript)
  const isOpen = useJarvisStore((state) => state.isTranscriptVisible)
  const endRef = useRef<HTMLDivElement>(null)
  const turns = groupTranscriptTurns(transcript)

  useEffect(() => {
    if (!isOpen) return
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    endRef.current?.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth' })
  }, [transcript.length, partialTranscript, isOpen])

  if (transcript.length === 0 && !partialTranscript) {
    return null
  }

  return (
    <div
      className={cn(
        'relative flex h-full flex-col overflow-hidden transition-[width,margin] ease-in-out',
        isOpen ? 'ml-6 w-[360px] delay-0 duration-200' : 'ml-0 w-0 delay-300 duration-200',
      )}
    >
      <div
        className={cn(
          'relative flex flex-1 flex-col overflow-hidden',
          isOpen ? 'pointer-events-auto' : 'pointer-events-none',
        )}
      >
        <div
          className={cn(
            'absolute top-0 right-0 flex w-px flex-col origin-top overflow-hidden opacity-40 transition-[height] ease-hologram',
            isOpen ? 'h-full delay-200 duration-500' : 'h-0 delay-0 duration-300',
          )}
        >
          <div className="h-3 shrink-0 bg-surface-highlight" />
          <div className="flex-1 bg-outline" />
          <div className="h-3 shrink-0 bg-surface-highlight" />
        </div>

        <div
          className={cn('flex h-full w-full flex-col', isOpen ? 'opacity-100' : 'opacity-0')}
          style={{
            clipPath: isOpen ? 'inset(0 0 0 0)' : 'inset(0 0 100% 0)',
            transition: isOpen
              ? 'opacity 0.3s ease-out 0.2s, clip-path 0.5s cubic-bezier(0.16, 1, 0.3, 1) 0.2s'
              : 'opacity 0.1s ease-out 0s, clip-path 0.3s ease-out 0s',
          }}
        >
          <div className="flex-1 overflow-y-auto px-0 pb-4" style={{ scrollbarWidth: 'thin' }}>
            <div className="flex flex-col">
              {turns.map((turn, index) => {
                const previous = turns[index - 1]
                const showBoundary = previous
                  ? startsNewConversation(turnTimestamp(previous), turnTimestamp(turn))
                  : false
                const notice = isUnsolicitedTurn(previous, turn)
                const spacing = index === 0 || showBoundary
                  ? undefined
                  : notice
                    ? 'mt-8'
                    : 'mt-6'

                return (
                  <React.Fragment key={turn.id}>
                    {showBoundary && <ConversationBoundary />}
                    <div className={spacing}>
                      {renderTurn(turn, notice)}
                    </div>
                  </React.Fragment>
                )
              })}

              {partialTranscript && (
                <div className={turns.length > 0 ? 'mt-6' : undefined}>
                  <SpokenTurn speaker="user" timestamp={Date.now()}>
                    <TextBody
                      item={{
                        id: 'partial',
                        text: partialTranscript,
                        sender: 'user',
                        type: 'text',
                        timestamp: Date.now(),
                        isPartial: true,
                      }}
                    />
                  </SpokenTurn>
                </div>
              )}
              <div ref={endRef} />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
