import React from 'react'
import { cn } from '../../utils/cn'

/** Shared status vocabulary with StatusDot (`off` collapses to neutral for labeled pills). */
export type StatusTone = 'success' | 'active' | 'warning' | 'error' | 'neutral' | 'off'

const toneClass: Record<StatusTone, string> = {
  success: 'border-status-success/40 bg-status-success/10 text-status-success',
  active: 'border-brand/40 bg-brand/10 text-brand-fg shadow-glow-brand-tight',
  warning: 'border-status-warning/40 bg-status-warning/10 text-status-warning',
  error: 'border-status-danger/40 bg-status-danger/10 text-status-danger-fg',
  neutral: 'border-outline/40 bg-surface/20 text-foreground-subtle',
  off: 'border-outline/25 bg-surface/10 text-foreground-subtle',
}

export interface StatusPillProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: StatusTone
  children: React.ReactNode
}

/** Compact semantic capsule for state labels and operational telemetry. */
export const StatusPill: React.FC<StatusPillProps> = ({
  tone = 'neutral',
  children,
  className,
  ...props
}) => (
  <span
    data-tone={tone}
    className={cn(
      'inline-flex max-w-full items-center rounded-full border px-2 py-0.5 font-mono text-meta font-medium uppercase tracking-[0.12em]',
      toneClass[tone],
      className,
    )}
    {...props}
  >
    {children}
  </span>
)
