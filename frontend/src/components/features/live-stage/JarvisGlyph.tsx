import React from 'react'
import { cn } from '../../../utils/cn'
import type { LiveStagePhase, LiveStageTone } from './liveStageState'
import './JarvisGlyph.css'

const toneClass: Record<LiveStageTone, string> = {
  neutral: 'text-foreground-subtle',
  brand: 'text-brand',
  output: 'text-brand-output',
  warning: 'text-status-warning',
  danger: 'text-status-danger-fg',
}

interface JarvisGlyphProps {
  phase: LiveStagePhase
  tone: LiveStageTone
}

/** Phases that reveal the grown outline over the idle shadow trace. */
const OUTLINE_EXPANDED_PHASES = new Set<LiveStagePhase>([
  'detected',
  'listening',
  'speaking',
])

/** Phases that keep the gradient fill lit — cross-fade between these via color. */
const FILL_ACTIVE_PHASES = new Set<LiveStagePhase>(['listening', 'speaking'])

// Figma geometry: base = idle shadow trace; active = ~2px outward outline.
const BASE_OUTER_ARC = 'M66 4.93945C79.4476 4.93946 92.5168 9.26345 103.181 17.2363C113.844 25.2089 121.505 36.3837 124.981 49.0244C128.457 61.6648 127.556 75.0697 122.416 87.1611C117.897 97.7923 110.321 106.888 100.645 113.382C98.8736 114.57 96.4503 113.957 95.3125 112.037C94.1599 110.092 94.8185 107.518 96.7656 106.154C104.708 100.592 110.939 92.9611 114.711 84.0879C119.156 73.6311 119.936 62.0356 116.93 51.1016C113.923 40.1676 107.298 30.5101 98.0889 23.625C88.8803 16.7403 77.6009 13.0107 66 13.0107C54.3991 13.0107 43.1197 16.7403 33.9111 23.625C24.7022 30.5101 18.0772 40.1676 15.0703 51.1016C12.0636 62.0356 12.8439 73.6311 17.2891 84.0879C21.0612 92.9611 27.2925 100.592 35.2344 106.154C37.1815 107.518 37.8401 110.092 36.6875 112.037C35.5497 113.957 33.1264 114.57 31.3555 113.382C21.6791 106.888 14.1035 97.7923 9.58398 87.1611C4.44374 75.0696 3.5425 61.6648 7.01855 49.0244C10.4948 36.3837 18.1559 25.2089 28.8193 17.2363C39.4832 9.26344 52.5524 4.93946 66 4.93945Z'
const BASE_INNER_ARC = 'M52.1963 86.4457C51.0488 88.479 48.5515 89.0495 46.9336 87.6908C43.1331 84.4985 40.1332 80.3816 38.2305 75.682C35.6951 69.4199 35.2499 62.4753 36.9648 55.9271C38.6798 49.3792 42.4578 43.5967 47.708 39.475C52.9581 35.3535 59.3882 33.1215 66 33.1215C72.6118 33.1215 79.0419 35.3535 84.292 39.475C89.5422 43.5967 93.3202 49.3792 95.0352 55.9271C96.7501 62.4753 96.3049 69.4199 93.7695 75.682C91.8668 80.3816 88.8669 84.4986 85.0664 87.6908C83.4485 89.0494 80.9512 88.4791 79.8037 86.4457C78.7311 84.5448 79.3693 82.0205 81.126 80.3314C83.3916 78.1529 85.1953 75.4966 86.3984 72.5248C88.2549 67.9392 88.5805 62.8561 87.3252 58.0629C86.0697 53.2693 83.3021 49.0285 79.4473 46.0023C75.592 42.9759 70.8651 41.3334 66 41.3334C61.1349 41.3334 56.408 42.9759 52.5527 46.0023C48.6979 49.0285 45.9303 53.2693 44.6748 58.0629C43.4196 62.8561 43.7451 67.9392 45.6016 72.5248C46.8048 75.4966 48.6083 78.1538 50.874 80.3324C52.6305 82.0215 53.2688 84.5449 52.1963 86.4457Z'
const ACTIVE_OUTER_ARC = 'M66 1C80.3151 1 94.2273 5.6027 105.579 14.0898C116.931 22.5768 125.087 34.4724 128.787 47.9287C132.487 61.3847 131.528 75.6547 126.056 88.5264C121.245 99.8433 113.18 109.526 102.88 116.438C100.995 117.704 98.4153 117.051 97.2041 115.007C95.9771 112.936 96.6772 110.196 98.75 108.744C107.204 102.823 113.838 94.7006 117.854 85.2549C122.586 74.1233 123.416 61.7793 120.215 50.1396C117.014 38.5004 109.962 28.2209 100.159 20.8916C90.3565 13.5626 78.3495 9.5918 66 9.5918C53.6505 9.5918 41.6435 13.5626 31.8408 20.8916C22.0378 28.2209 14.986 38.5004 11.7852 50.1396C8.58426 61.7793 9.4144 74.1233 14.1465 85.2549C18.162 94.7006 24.7957 102.823 33.25 108.744C35.3228 110.196 36.0228 112.936 34.7959 115.007C33.5847 117.051 31.0054 117.704 29.1201 116.438C18.8196 109.526 10.7554 99.8433 5.94434 88.5264C0.472428 75.6547 -0.487502 61.3847 3.21289 47.9287C6.9134 34.4724 15.0695 22.5768 26.4209 14.0898C37.7727 5.60272 51.6849 1 66 1Z'
const ACTIVE_INNER_ARC = 'M51.3057 87.7646C50.0841 89.9293 47.4254 90.5364 45.7031 89.0898C41.6574 85.6916 38.464 81.3094 36.4385 76.3066C33.7395 69.6404 33.2661 62.2472 35.0918 55.2764C36.9175 48.306 40.9394 42.1513 46.5283 37.7637C52.1171 33.3763 58.9616 31 66 31C73.0384 31 79.8829 33.3763 85.4717 37.7637C91.0606 42.1513 95.0825 48.306 96.9082 55.2764C98.7339 62.2472 98.2605 69.6404 95.5615 76.3066C93.536 81.3094 90.3426 85.6916 86.2969 89.0898C84.5746 90.5365 81.9159 89.9293 80.6943 87.7646C79.5526 85.7413 80.2318 83.0549 82.1016 81.2568C84.5134 78.9378 86.433 76.1097 87.7139 72.9463C89.6903 68.0647 90.0365 62.6534 88.7002 57.5508C87.3637 52.4479 84.418 47.9343 80.3145 44.7129C76.2105 41.4911 71.179 39.7422 66 39.7422C60.821 39.7422 55.7895 41.4911 51.6856 44.7129C47.582 47.9343 44.6363 52.4479 43.2998 57.5508C41.9635 62.6534 42.3097 68.0647 44.2861 72.9463C45.567 76.1097 47.4866 78.9378 49.8984 81.2568C51.7682 83.0549 52.4473 85.7413 51.3057 87.7646Z'

export const JarvisGlyph: React.FC<JarvisGlyphProps> = ({
  phase,
  tone,
}) => {
  const outerGradientId = React.useId()
  const innerGradientId = React.useId()
  const outlineExpanded = OUTLINE_EXPANDED_PHASES.has(phase)
  const fillActive = FILL_ACTIVE_PHASES.has(phase)

  return (
    <div
      className={cn('jarvis-glyph', toneClass[tone])}
      data-phase={phase}
      data-outline-expanded={outlineExpanded || undefined}
      data-fill-active={fillActive || undefined}
      aria-hidden
    >
      <svg viewBox="-12 -12 156 156" fill="none">
        <defs>
          <linearGradient id={outerGradientId} x1="66" y1="120" x2="66" y2="0" gradientUnits="userSpaceOnUse">
            <stop className="jarvis-glyph__grad-foot" stopColor="currentColor" stopOpacity="0.1" />
            <stop className="jarvis-glyph__grad-head" offset="1" stopColor="currentColor" stopOpacity="0.7" />
          </linearGradient>
          <linearGradient id={innerGradientId} x1="66" y1="30" x2="66" y2="93" gradientUnits="userSpaceOnUse">
            <stop className="jarvis-glyph__grad-head" stopColor="currentColor" stopOpacity="0.7" />
            <stop className="jarvis-glyph__grad-foot" offset="1" stopColor="currentColor" stopOpacity="0.1" />
          </linearGradient>
        </defs>

        <g className="jarvis-glyph__base">
          <path d={BASE_OUTER_ARC} />
          <path d={BASE_INNER_ARC} />
        </g>

        <g className="jarvis-glyph__glow">
          <path d={ACTIVE_OUTER_ARC} />
          <path d={ACTIVE_INNER_ARC} />
        </g>

        <g className="jarvis-glyph__fill">
          <path d={ACTIVE_OUTER_ARC} fill={`url(#${outerGradientId})`} />
          <path d={ACTIVE_INNER_ARC} fill={`url(#${innerGradientId})`} />
        </g>

        <g className="jarvis-glyph__outline">
          <path d={ACTIVE_OUTER_ARC} pathLength="1" />
          <path d={ACTIVE_INNER_ARC} pathLength="1" />
        </g>

        <g className="jarvis-glyph__scan">
          <path d={BASE_OUTER_ARC} pathLength="100" />
          <path d={BASE_INNER_ARC} pathLength="100" />
        </g>
      </svg>
    </div>
  )
}
