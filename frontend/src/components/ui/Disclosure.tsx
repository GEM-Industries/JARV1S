import React, { useState } from 'react'
import { CaretRightIcon } from '@phosphor-icons/react'
import { cn } from '../../utils/cn'

interface DisclosureProps {
  label: React.ReactNode
  children: React.ReactNode
  trailing?: React.ReactNode
  defaultOpen?: boolean
  variant?: 'plain' | 'surface'
  className?: string
  summaryClassName?: string
  contentClassName?: string
}

export const Disclosure: React.FC<DisclosureProps> = ({
  label,
  children,
  trailing,
  defaultOpen = false,
  variant = 'plain',
  className,
  summaryClassName,
  contentClassName,
}) => {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <details
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
      className={cn(variant === 'surface' && 'rounded-control bg-surface/20 px-3', className)}
    >
      <summary
        className={cn(
          'flex min-h-10 cursor-pointer list-none items-center justify-between gap-3 rounded-control py-2',
          'type-label-small text-foreground-muted transition-colors duration-feedback hover:text-foreground',
          'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/60 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas',
          '[&::-webkit-details-marker]:hidden',
          summaryClassName,
        )}
      >
        <span className="inline-flex min-w-0 items-center gap-2">
          <CaretRightIcon
            size={12}
            weight="bold"
            className={cn(
              'shrink-0 text-foreground-subtle transition-transform duration-feedback ease-hologram motion-reduce:transition-none',
              open && 'rotate-90 text-brand',
            )}
            aria-hidden
          />
          <span className="min-w-0">{label}</span>
        </span>
        {trailing}
      </summary>
      <div className={contentClassName}>{children}</div>
    </details>
  )
}
