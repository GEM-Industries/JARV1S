import React from 'react'
import { cn } from '../../../utils/cn'
import type { CapabilityLaneStatus } from '../../../client/setupApi'

export const LANE_STATUS_STYLES: Record<string, string> = {
  ready: 'border-status-success/40 text-status-success bg-status-success/10',
  configured: 'border-brand/40 text-brand bg-brand/10',
  needs_action: 'border-outline/30 text-foreground-subtle',
  optional: 'border-outline/30 text-foreground-subtle',
  degraded: 'border-status-warning/40 text-status-warning bg-status-warning/10',
  unavailable: 'border-outline/20 text-foreground-disabled',
}

interface CapabilityLanesProps {
  lanes: CapabilityLaneStatus[]
  className?: string
}

export const CapabilityLanes: React.FC<CapabilityLanesProps> = ({ lanes, className }) => {
  if (!lanes.length) return null

  return (
    <div className={cn('border-t border-outline/20 pt-4', className)}>
      <p className="type-heading text-foreground mb-2">Capabilities</p>
      <div className="flex flex-wrap gap-2">
        {lanes.map((lane) => (
          <span
            key={lane.id}
            className={cn(
              'rounded-full border px-3 py-1 type-label-small',
              LANE_STATUS_STYLES[lane.status] ?? LANE_STATUS_STYLES.optional,
            )}
            title={lane.detail ?? undefined}
          >
            {lane.label}
            {lane.status === 'ready' ? ' · ready' : ''}
          </span>
        ))}
      </div>
    </div>
  )
}
