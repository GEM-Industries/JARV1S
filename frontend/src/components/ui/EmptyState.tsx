import React from 'react'
import { cn } from '../../utils/cn'

export interface EmptyStateProps {
  title: string
  description?: string
  icon?: React.ReactNode
  action?: React.ReactNode
  tone?: 'muted' | 'error'
  className?: string
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  title,
  description,
  icon,
  action,
  tone = 'muted',
  className,
}) => (
  <div
    className={cn(
      'flex min-h-40 flex-col items-center justify-center rounded-panel bg-surface/[0.08] px-6 py-8 text-center',
      className,
    )}
  >
    {icon && (
      <div
        className={cn(
          'mb-4 flex h-11 w-11 items-center justify-center rounded-full bg-surface/20',
          tone === 'error' ? 'text-status-danger' : 'text-foreground-subtle',
        )}
        aria-hidden
      >
        {icon}
      </div>
    )}
    <h3 className={cn('type-heading', tone === 'error' ? 'text-status-danger' : 'text-foreground')}>
      {title}
    </h3>
    {description && (
      <p className="mt-2 max-w-md type-body text-foreground-muted">
        {description}
      </p>
    )}
    {action && <div className="mt-5">{action}</div>}
  </div>
)
