import type { ActivityEntry } from '../../../types/operations'

type Outcome = ActivityEntry['outcome']

/**
 * Single source of truth for outcome → visual treatment. Drives the row accent,
 * status dot, and any textual outcome label so colour semantics never drift.
 * Glow is reserved for "live" outcomes (running) per the JARV1S style guide.
 */
type OutcomeTone = 'success' | 'active' | 'warning' | 'error'

export const OUTCOME_META: Record<
  Outcome,
  { label: string; rail: string; node: string; tone: OutcomeTone }
> = {
  succeeded: {
    label: 'Succeeded',
    rail: 'from-status-success/40',
    node: 'border-status-success/50 bg-status-success/70',
    tone: 'success',
  },
  running: {
    label: 'Running',
    rail: 'from-brand/60',
    node: 'border-brand bg-brand shadow-glow-brand-tight',
    tone: 'active',
  },
  waiting: {
    label: 'Waiting',
    rail: 'from-status-warning/50',
    node: 'border-status-warning/70 bg-status-warning/70',
    tone: 'warning',
  },
  failed: {
    label: 'Failed',
    rail: 'from-status-danger/40',
    node: 'border-status-danger/60 bg-status-danger/60',
    tone: 'error',
  },
  suppressed: {
    label: 'Suppressed',
    rail: 'from-status-warning/40',
    node: 'border-status-warning/55 bg-status-warning/50',
    tone: 'warning',
  },
  cancelled: {
    label: 'Cancelled',
    rail: 'from-text-disabled/35',
    node: 'border-text-disabled/45 bg-foreground-disabled/40',
    tone: 'warning',
  },
}
