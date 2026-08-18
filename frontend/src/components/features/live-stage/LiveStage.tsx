import React from 'react'
import { cn } from '../../../utils/cn'
import type { LiveStagePresentation, LiveStageTone } from './liveStageState'
import { JarvisGlyph } from './JarvisGlyph'

const toneClass: Record<LiveStageTone, string> = {
  neutral: 'text-foreground-muted',
  brand: 'text-brand-fg',
  output: 'text-brand-output',
  warning: 'text-status-warning',
  danger: 'text-status-danger-fg',
}

interface LiveStageProjectionProps {
  presentation: LiveStagePresentation
  showPreview?: boolean
}

/**
 * Stable focal projection for voice/recovery phases.
 * Widgets replace this slot in PrimaryCanvas when focalKind === 'widget'.
 * Fixed slots preserve spatial continuity while optional copy fades in place.
 */
export const LiveStageProjection: React.FC<LiveStageProjectionProps> = ({
  presentation,
  showPreview = true,
}) => {
  const {
    phase,
    label,
    detail,
    tone,
    userPreview,
    assistantPreview,
    settled,
  } = presentation
  const preview = userPreview || assistantPreview
  // Idle status lives in the dashboard chrome; stage labels are for active/recovery work.
  const showLabel = phase !== 'idle'
  const showDetail = Boolean(detail) && showLabel
  const showPreviewText = Boolean(preview) && showPreview

  return (
    <div
      className={cn(
        'pointer-events-none grid h-80 w-full max-w-[48rem]',
        'grid-rows-[auto_1.25rem_auto_1fr] justify-items-center content-start text-center',
        'motion-safe:animate-in motion-safe:fade-in',
        'motion-safe:duration-transition motion-safe:ease-hologram',
      )}
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-phase={phase}
      data-settled={settled || undefined}
    >
      <div className="relative flex items-center justify-center pb-2">
        <JarvisGlyph phase={phase} tone={tone} />
      </div>

      <p
        key={phase}
        className={cn(
          'type-label',
          toneClass[tone],
          !showLabel && 'opacity-0',
          'motion-safe:animate-in motion-safe:fade-in motion-safe:duration-feedback',
          'motion-safe:transition-opacity motion-safe:duration-feedback',
        )}
        aria-hidden={!showLabel}
      >
        {label}
      </p>

      <div
        className={cn(
          'flex w-full items-start justify-center overflow-hidden',
          showDetail ? 'min-h-4 pt-1' : 'h-0 min-h-0 p-0',
        )}
        aria-hidden={!showDetail}
      >
        {showDetail ? (
          <p className="max-w-[40ch] type-meta text-foreground-subtle">
            {detail}
          </p>
        ) : null}
      </div>

      <div className="flex w-full items-start justify-center overflow-hidden pt-4">
        <p
          className={cn(
            'max-w-[60ch] type-body-reading text-balance',
            assistantPreview ? 'text-foreground' : 'text-foreground-muted',
            userPreview && 'text-brand-fg',
            settled && !userPreview && 'text-foreground-muted',
            !showPreviewText && 'opacity-0',
            'motion-safe:transition-[color,opacity] motion-safe:duration-transition',
          )}
          aria-hidden={!showPreviewText}
        >
          {preview ?? '\u00a0'}
        </p>
      </div>
    </div>
  )
}
