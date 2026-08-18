import React from 'react'
import { cn } from '../../utils/cn'

export interface SwitchProps {
  checked: boolean
  onChange: (checked: boolean) => void
  label: string
  description?: string
  disabled?: boolean
  className?: string
}

export const Switch: React.FC<SwitchProps> = ({
  checked,
  onChange,
  label,
  description,
  disabled = false,
  className,
}) => {
  const descriptionId = React.useId()

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-describedby={description ? descriptionId : undefined}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'flex min-h-11 w-full items-center justify-between gap-4 rounded-control text-left',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/65 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
    >
      <span className="min-w-0">
        <span className="block type-label text-foreground">{label}</span>
        {description && (
          <span id={descriptionId} className="mt-1 block type-body text-foreground-muted">
            {description}
          </span>
        )}
      </span>
      <span
        aria-hidden
        className={cn(
          'relative h-6 w-11 shrink-0 rounded-control border transition-colors duration-feedback ease-hologram motion-reduce:transition-none',
          checked
            ? 'border-brand/70 bg-brand/15'
            : 'border-outline/45 bg-canvas/60',
        )}
      >
        <span
          className={cn(
            'absolute top-0.5 h-[1.125rem] w-[1.125rem] rounded-control border transition-all duration-feedback ease-hologram motion-reduce:transition-none',
            checked
              ? 'left-[1.25rem] border-brand/50 bg-brand'
              : 'left-0.5 border-outline/35 bg-foreground-subtle',
          )}
        />
      </span>
    </button>
  )
}
