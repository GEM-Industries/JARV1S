import React from 'react'
import { cn } from '../../utils/cn'

export interface ChipProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  selected?: boolean
}

/** Selectable pill for filter and toggle groups. Use SegmentedTabs for tabs. */
export const Chip = React.forwardRef<HTMLButtonElement, ChipProps>(
  ({ selected = false, disabled, className, type = 'button', children, ...props }, ref) => (
    <button
      ref={ref}
      type={type}
      disabled={disabled}
      aria-pressed={selected}
      data-selected={selected ? '' : undefined}
      data-disabled={disabled ? '' : undefined}
      className={cn(
        'min-h-10 rounded-full border border-transparent px-4 py-2 type-label transition-[background-color,border-color,color,transform] duration-feedback ease-hologram active:scale-[0.98]',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/60',
        'disabled:cursor-not-allowed disabled:opacity-50 disabled:pointer-events-none',
        selected
          ? 'border-brand/30 bg-gradient-to-b from-brand/20 via-brand/12 to-brand/7 text-brand-fg'
          : 'bg-surface/15 text-foreground-subtle hover:bg-surface/30 hover:text-foreground-muted',
        className,
      )}
      {...props}
    >
      {children}
    </button>
  ),
)

Chip.displayName = 'Chip'
