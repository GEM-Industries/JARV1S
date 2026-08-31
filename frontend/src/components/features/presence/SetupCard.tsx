import React from 'react'
import { CaretDownIcon } from '@phosphor-icons/react'

interface SetupCardProps {
  icon: React.ReactNode
  title: string
  subtitle: string
  expanded: boolean
  onToggle: () => void
  children: React.ReactNode
}

/** Collapsed setup row that expands into a pairing or install flow. */
export const SetupCard: React.FC<SetupCardProps> = ({
  icon,
  title,
  subtitle,
  expanded,
  onToggle,
  children,
}) => (
  <section className="overflow-hidden rounded-panel bg-surface/20">
    <button
      type="button"
      aria-expanded={expanded}
      onClick={onToggle}
      className="flex min-h-12 w-full items-center gap-3 px-4 py-3 text-left transition-colors duration-feedback hover:bg-surface/30 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70"
    >
      {icon}
      <span className="min-w-0 flex-1">
        <span className="block type-label text-foreground">{title}</span>
        <span className="block truncate type-meta text-foreground-subtle">{subtitle}</span>
      </span>
      <CaretDownIcon
        size={14}
        className={`shrink-0 text-foreground-subtle transition-transform duration-feedback ease-hologram motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`}
      />
    </button>
    {expanded && (
      <div className="flex flex-col gap-4 border-t border-outline/15 px-4 py-4">{children}</div>
    )}
  </section>
)
