import React from 'react'
import { cn } from '../../utils/cn'

export interface PlaceholderProps {
  tone?: 'muted' | 'error'
  children: React.ReactNode
  className?: string
}

/** Compact inline message for loading/empty/error inside an already-framed panel.
 * Prefer `EmptyState` when the surface needs title + description + recovery action. */
export const Placeholder: React.FC<PlaceholderProps> = ({ tone = 'muted', children, className }) => (
  <div
    className={cn(
      'rounded-control bg-surface/25 px-4 py-3 text-center type-body overflow-hidden',
      tone === 'error' ? 'text-status-danger-fg' : 'text-foreground-subtle',
      className,
    )}
  >
    {children}
  </div>
)
