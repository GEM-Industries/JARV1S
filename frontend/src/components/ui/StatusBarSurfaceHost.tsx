import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { cn } from '../../utils/cn'
import { Hologram } from './Hologram'

export type StatusBarSurfaceKind = 'menu' | 'workspace'
export type StatusBarSurfaceSize =
  | 'compact'
  | 'standard'
  | 'wide'
  | 'expanded'
  | 'workspace-narrow'
  | 'workspace'

export interface StatusBarSurface {
  id: React.Key
  kind: StatusBarSurfaceKind
  size: StatusBarSurfaceSize
  label: string
  color?: 'default' | 'warning' | 'error' | 'inactive'
  role?: 'menu' | 'region'
  children: React.ReactNode
}

export interface StatusBarSurfaceHostProps {
  surface: StatusBarSurface | null
  onClose: () => void
}

const CONTENT_EXIT_MS = 100
const CONTENT_ENTER_DELAY_MS = 80
const GEOMETRY_MORPH_MS = 300
const SURFACE_WIDTHS: Record<StatusBarSurfaceSize, number | string> = {
  compact: 224,
  standard: 288,
  wide: 320,
  expanded: 384,
  'workspace-narrow': 'min(640px, calc(100vw - 3rem))',
  workspace: 'min(920px, calc(100vw - 3rem))',
}

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * Persistent top-right StatusBar surface.
 * It keeps shell geometry mounted while menu and workspace destinations change.
 */
export const StatusBarSurfaceHost: React.FC<StatusBarSurfaceHostProps> = ({
  surface,
  onClose,
}) => {
  const [renderedSurface, setRenderedSurface] = useState(surface)
  const [mounted, setMounted] = useState(surface !== null)
  const [visible, setVisible] = useState(false)
  const [contentVisible, setContentVisible] = useState(false)
  const [menuHeight, setMenuHeight] = useState<number>()
  // Height/width morph only between destinations — not when menu content reflows (e.g. async load).
  const [morphGeometry, setMorphGeometry] = useState(false)
  const frameRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const delayContentEntry = useRef(false)
  const latestSurface = useRef(surface)
  latestSurface.current = surface
  const displayedSurface = surface?.id === renderedSurface?.id ? surface : renderedSurface

  useEffect(() => {
    if (!surface) {
      setContentVisible(false)
      setVisible(false)
      if (prefersReducedMotion()) {
        setMounted(false)
        setRenderedSurface(null)
        setMenuHeight(undefined)
        setMorphGeometry(false)
      }
      return
    }

    setMounted(true)

    if (!renderedSurface) {
      delayContentEntry.current = false
      setMorphGeometry(false)
      setRenderedSurface(surface)
      return
    }

    if (renderedSurface.id === surface.id) return

    if (prefersReducedMotion()) {
      delayContentEntry.current = false
      setMorphGeometry(false)
      setRenderedSurface(surface)
      setVisible(true)
      setContentVisible(true)
      return
    }

    setContentVisible(false)
    const timeout = window.setTimeout(() => {
      const nextSurface = latestSurface.current
      if (!nextSurface) return
      if (nextSurface.kind === 'menu' && frameRef.current) {
        setMenuHeight(frameRef.current.getBoundingClientRect().height)
      }
      delayContentEntry.current = (
        renderedSurface.kind !== nextSurface.kind
        || renderedSurface.size !== nextSurface.size
      )
      setMorphGeometry(true)
      setRenderedSurface(nextSurface)
    }, CONTENT_EXIT_MS)
    return () => window.clearTimeout(timeout)
  }, [renderedSurface?.id, surface?.id])

  useLayoutEffect(() => {
    if (!displayedSurface || displayedSurface.kind !== 'menu' || !contentRef.current) return

    const updateHeight = () => {
      if (!contentRef.current) return
      setMenuHeight(contentRef.current.scrollHeight)
    }
    updateHeight()

    const observer = new ResizeObserver(updateHeight)
    observer.observe(contentRef.current)
    return () => observer.disconnect()
  }, [displayedSurface?.id])

  useEffect(() => {
    if (!mounted || !displayedSurface) return
    let innerFrame = 0
    const outerFrame = requestAnimationFrame(() => {
      innerFrame = requestAnimationFrame(() => {
        setVisible(true)
        setContentVisible(true)
      })
    })
    return () => {
      cancelAnimationFrame(outerFrame)
      cancelAnimationFrame(innerFrame)
    }
  }, [displayedSurface?.id, mounted])

  useEffect(() => {
    if (!morphGeometry) return
    const timeout = window.setTimeout(() => setMorphGeometry(false), GEOMETRY_MORPH_MS)
    return () => window.clearTimeout(timeout)
  }, [morphGeometry, displayedSurface?.id])

  useEffect(() => {
    if (!surface) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onClose()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose, surface?.id])

  if (!mounted || !displayedSurface) return null

  const workspace = displayedSurface.kind === 'workspace'
  const width = SURFACE_WIDTHS[displayedSurface.size]
  const height = workspace
    ? 'calc(100vh - var(--shell-overlay-top) - var(--safe-area-bottom))'
    : menuHeight

  return (
    <>
      <div
        className="pointer-events-auto fixed inset-0 z-40"
        onPointerDown={onClose}
        aria-hidden
      />
      <div
        ref={frameRef}
        role={workspace ? 'dialog' : displayedSurface.role ?? 'region'}
        aria-modal={workspace ? 'false' : undefined}
        aria-label={displayedSurface.label}
        onTransitionEnd={(event) => {
          if (event.target !== event.currentTarget || event.propertyName !== 'opacity') return
          if (!visible) {
            setMounted(false)
            setRenderedSurface(null)
            setMenuHeight(undefined)
            setMorphGeometry(false)
            delayContentEntry.current = false
          }
        }}
        style={{ width, height }}
        className={cn(
          'pointer-events-auto fixed right-6 top-shell-overlay z-50 overflow-hidden rounded-shell bg-canvas [corner-shape:squircle]',
          '[contain:layout_paint] duration-transition ease-hologram',
          morphGeometry
            ? 'transition-[width,height,transform,opacity]'
            : 'transition-[transform,opacity]',
          'motion-reduce:transition-none motion-reduce:scale-100',
          visible
            ? 'scale-100 opacity-100'
            : 'scale-[0.985] opacity-0 duration-feedback ease-in',
        )}
      >
        <Hologram
          aria-hidden
          variant="base"
          color={displayedSurface.color ?? 'default'}
          className={cn(
            'pointer-events-none absolute inset-0 z-20 h-full w-full transition-opacity duration-feedback',
            workspace ? 'opacity-100' : 'opacity-0',
          )}
        />
        <Hologram
          aria-hidden
          variant="corners"
          color={displayedSurface.color ?? 'default'}
          className={cn(
            'pointer-events-none absolute inset-0 z-20 h-full w-full transition-opacity duration-feedback',
            workspace ? 'opacity-0' : 'opacity-100',
          )}
        />
        <div
          ref={contentRef}
          key={displayedSurface.id}
          style={{
            width,
            transitionDelay: contentVisible && delayContentEntry.current
              ? `${CONTENT_ENTER_DELAY_MS}ms`
              : '0ms',
          }}
          className={cn(
            'absolute right-0 top-0 z-10 min-h-0 transition-opacity motion-reduce:transition-none',
            workspace ? 'flex h-full flex-col' : 'p-2',
            contentVisible
              ? 'opacity-100 duration-feedback ease-hologram'
              : 'opacity-0 duration-instant ease-in',
          )}
        >
          {displayedSurface.children}
        </div>
      </div>
    </>
  )
}
