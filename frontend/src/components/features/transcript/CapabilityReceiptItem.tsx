import React from 'react'
import { CaretRightIcon } from '@phosphor-icons/react'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { cn } from '../../../utils/cn'
import { TranscriptItem as TranscriptItemType } from '../../../types'
import { StatusDot } from '../../ui/StatusDot'
import { TextLink } from '../../ui/TextLink'
import { buildReceipt, hostnameOf } from './capabilityReceipt'

interface CapabilityReceiptItemProps {
  item: TranscriptItemType
}

export const CapabilityReceiptItem: React.FC<CapabilityReceiptItemProps> = ({ item }) => {
  const { id, code, codeResult, status, isCollapsed } = item
  const toggleCollapse = useJarvisStore((state) => state.toggleTranscriptItemCollapse)
  const collapsed = isCollapsed ?? true
  const receipt = buildReceipt(code, codeResult, status)
  const isError = receipt.statusLabel === 'Failed'
  const isRunning = receipt.statusLabel === 'Running'

  return (
    <div
      className={cn(
        'overflow-hidden rounded-panel border bg-surface/20 transition-[border-color] duration-feedback',
        isError ? 'border-status-danger/40' : isRunning ? 'border-brand/35' : 'border-outline/50',
      )}
    >
      <button
        type="button"
        onClick={() => toggleCollapse(id)}
        aria-expanded={!collapsed}
        className={cn(
          'flex min-h-10 w-full items-center gap-3 px-3 py-2 text-left',
          'transition-colors duration-feedback hover:bg-surface/20',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand/60',
          isRunning && 'bg-brand/10 hover:bg-brand/20',
        )}
      >
        <div className="flex min-w-0 flex-1 items-start gap-2">
          {(isRunning || isError) && (
            <span className="flex h-5 shrink-0 items-center">
              <StatusDot
                status={isError ? 'error' : 'active'}
                size="md"
                className={cn(isRunning && 'animate-pulse motion-reduce:animate-none')}
              />
            </span>
          )}
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate type-label text-foreground">{receipt.title}</span>
              {receipt.statusLabel && (
                <span
                  className={cn(
                    'shrink-0 type-meta',
                    isError ? 'text-status-danger' : 'text-brand-fg',
                  )}
                >
                  {receipt.statusLabel}
                </span>
              )}
            </div>
            {collapsed && receipt.subtitle && (
              <p className="truncate type-meta text-foreground-subtle">{receipt.subtitle}</p>
            )}
          </div>
        </div>
        <CaretRightIcon
          size={12}
          weight="bold"
          className={cn(
            'shrink-0 text-foreground-subtle transition-transform duration-feedback ease-hologram motion-reduce:transition-none',
            !collapsed && 'rotate-90',
          )}
          aria-hidden
        />
      </button>

      {!collapsed && (
        <div className="flex flex-col gap-3 border-t border-outline/50 px-3 py-3">
          {receipt.facts.length > 0 && (
            <dl className="grid grid-cols-[5rem_minmax(0,1fr)] gap-x-3 gap-y-2">
              {receipt.facts.map((fact) => (
                <React.Fragment key={fact.key}>
                  <dt className="type-meta text-foreground-subtle">{fact.key}</dt>
                  <dd className="min-w-0 break-words type-body text-foreground">{fact.value}</dd>
                </React.Fragment>
              ))}
            </dl>
          )}

          {receipt.links && (
            <ul className="flex flex-col gap-2">
              {receipt.links.map((hit, index) => (
                <li key={`${hit.title}-${index}`} className="flex min-w-0 flex-col gap-1">
                  {hit.url ? (
                    <TextLink href={hit.url} external>
                      {hit.title}
                    </TextLink>
                  ) : (
                    <p className="type-body text-foreground">{hit.title}</p>
                  )}
                  {hostnameOf(hit.url) && (
                    <p className="type-meta text-foreground-subtle">{hostnameOf(hit.url)}</p>
                  )}
                </li>
              ))}
            </ul>
          )}

          {receipt.output && (
            <p
              className={cn(
                'max-h-40 overflow-auto whitespace-pre-wrap break-words',
                receipt.outputKind === 'json'
                  ? 'font-mono text-meta leading-relaxed text-foreground-muted'
                  : 'type-body text-foreground-muted',
                isError && 'text-status-danger',
              )}
            >
              {receipt.output}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
