import React, { useId, useRef } from 'react'
import { cn } from '../../utils/cn'

export interface SegmentedTab<T extends string> {
  value: T
  label: string
  count?: number
}

export interface SegmentedTabsProps<T extends string> {
  label: string
  tabs: Array<SegmentedTab<T>>
  value: T
  onChange: (value: T) => void
  className?: string
  idPrefix?: string
}

export function SegmentedTabs<T extends string>({
  label,
  tabs,
  value,
  onChange,
  className,
  idPrefix,
}: SegmentedTabsProps<T>) {
  const generatedId = useId().replace(/:/g, '')
  const prefix = idPrefix ?? generatedId
  const refs = useRef<Array<HTMLButtonElement | null>>([])

  const move = (index: number, direction: 1 | -1) => {
    const next = (index + direction + tabs.length) % tabs.length
    onChange(tabs[next].value)
    refs.current[next]?.focus()
  }

  return (
    <div
      role="tablist"
      aria-label={label}
      className={cn(
        'corner-squircle inline-flex min-h-11 items-center gap-0.5 rounded-[calc(var(--radius-panel)+4px)] border border-outline/15 bg-surface/15 p-0.5',
        className,
      )}
    >
      {tabs.map((tab, index) => {
        const selected = tab.value === value
        return (
          <button
            key={tab.value}
            ref={(node) => {
              refs.current[index] = node
            }}
            id={`${prefix}-tab-${tab.value}`}
            type="button"
            role="tab"
            tabIndex={selected ? 0 : -1}
            aria-selected={selected}
            aria-controls={`${prefix}-panel-${tab.value}`}
            onClick={() => onChange(tab.value)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowRight') {
                event.preventDefault()
                move(index, 1)
              } else if (event.key === 'ArrowLeft') {
                event.preventDefault()
                move(index, -1)
              } else if (event.key === 'Home') {
                event.preventDefault()
                onChange(tabs[0].value)
                refs.current[0]?.focus()
              } else if (event.key === 'End') {
                event.preventDefault()
                onChange(tabs[tabs.length - 1].value)
                refs.current[tabs.length - 1]?.focus()
              }
            }}
            className={cn(
              'corner-squircle flex min-h-10 items-center gap-2 rounded-[calc(var(--radius-panel)+2px)] border px-4 type-label transition-colors duration-feedback',
              'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/65',
              selected
                ? 'border-brand/30 bg-gradient-to-b from-brand/16 to-brand/8 text-brand-fg'
                : 'border-transparent text-foreground-subtle hover:bg-surface/30 hover:text-foreground',
            )}
          >
            {tab.label}
            {tab.count !== undefined && (
              <span className={cn('type-meta tabular-nums', selected ? 'text-brand-fg' : 'text-foreground-subtle')}>
                {tab.count}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}

export interface TabPanelProps extends React.HTMLAttributes<HTMLDivElement> {
  tab: string
  idPrefix: string
  active: boolean
}

export const TabPanel: React.FC<TabPanelProps> = ({
  tab,
  idPrefix,
  active,
  className,
  children,
  ...props
}) => (
  <div
    id={`${idPrefix}-panel-${tab}`}
    role="tabpanel"
    aria-labelledby={`${idPrefix}-tab-${tab}`}
    hidden={!active}
    tabIndex={0}
    className={cn('min-h-0 outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand/45', className)}
    {...props}
  >
    {children}
  </div>
)
