import { useEffect } from 'react'
import { useJarvisStore } from '../../../store/useJarvisStore'
import type { LiveStagePresentation } from './liveStageState'

const MIN_DWELL_MS = 5_000
const MAX_DWELL_MS = 15_000

const responseDwellMs = (text: string): number => {
  const wordCount = text.trim().split(/\s+/).filter(Boolean).length
  return Math.min(MAX_DWELL_MS, Math.max(MIN_DWELL_MS, 2_000 + wordCount * 300))
}

/**
 * Shows stage preview copy unless the transcript rail owns the reading surface.
 * After a just-finished reply, dwell then clears the ephemeral scratch so the
 * stage returns to ready without resurrecting history.
 */
export const useLiveStagePreview = (
  presentation: LiveStagePresentation,
  transcriptVisible: boolean,
): boolean => {
  const clearLiveAssistantPreview = useJarvisStore((s) => s.clearLiveAssistantPreview)
  const { assistantPreview, assistantResponseKey, phase } = presentation
  const assistantIsSpeaking = phase === 'speaking'

  useEffect(() => {
    if (!assistantResponseKey || assistantIsSpeaking || !assistantPreview) return

    const timeout = window.setTimeout(
      () => clearLiveAssistantPreview(),
      responseDwellMs(assistantPreview),
    )
    return () => window.clearTimeout(timeout)
  }, [assistantIsSpeaking, assistantPreview, assistantResponseKey, clearLiveAssistantPreview])

  return !transcriptVisible
}
