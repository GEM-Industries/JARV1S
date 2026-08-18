import { useEffect, useRef, useState } from 'react'
import { cn } from '../../utils/cn'

/** Match StatusBarSurfaceHost content fade-through. */
const CONTENT_EXIT_MS = 100

/** `1` = drill in (enter from right), `-1` = back (enter from left). */
export type FadeThroughDirection = 1 | -1

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * Fade-through when a view key changes — same rhythm as StatusBarSurfaceHost
 * destination swaps: brief exit, swap while hidden, fade (+ optional slide) in.
 */
export function useFadeThrough<T>(
  active: T,
  direction: FadeThroughDirection = 1,
): {
  rendered: T
  className: string
} {
  const [rendered, setRendered] = useState(active)
  const [visible, setVisible] = useState(true)
  /** `0` while exiting; ±1 after swap so the enter slide starts from the right place. */
  const [enterFrom, setEnterFrom] = useState<0 | FadeThroughDirection>(0)
  const latest = useRef(active)
  const directionRef = useRef(direction)
  latest.current = active
  directionRef.current = direction

  useEffect(() => {
    if (Object.is(active, rendered)) return

    if (prefersReducedMotion()) {
      setEnterFrom(0)
      setRendered(active)
      setVisible(true)
      return
    }

    setEnterFrom(0)
    setVisible(false)
    const timeout = window.setTimeout(() => {
      setEnterFrom(directionRef.current)
      setRendered(latest.current)
    }, CONTENT_EXIT_MS)
    return () => window.clearTimeout(timeout)
  }, [active, rendered])

  useEffect(() => {
    if (!Object.is(active, rendered)) return
    let innerFrame = 0
    const outerFrame = requestAnimationFrame(() => {
      innerFrame = requestAnimationFrame(() => setVisible(true))
    })
    return () => {
      cancelAnimationFrame(outerFrame)
      cancelAnimationFrame(innerFrame)
    }
  }, [active, rendered])

  return {
    rendered,
    className: cn(
      'transition-[opacity,transform] motion-reduce:transition-none motion-reduce:translate-x-0',
      visible
        ? 'translate-x-0 opacity-100 duration-feedback ease-hologram'
        : cn(
            'opacity-0 duration-instant ease-in pointer-events-none',
            enterFrom === 1 && 'translate-x-2',
            enterFrom === -1 && '-translate-x-2',
          ),
    ),
  }
}
