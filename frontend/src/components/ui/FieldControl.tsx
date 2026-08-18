import React, { forwardRef, useId } from 'react'
import { MagnifyingGlassIcon, XIcon } from '@phosphor-icons/react'
import { cn } from '../../utils/cn'

export interface FieldControlProps {
  label: string
  hint?: string
  htmlFor?: string
  className?: string
  labelHidden?: boolean
  children: React.ReactNode
}

export const FieldControl: React.FC<FieldControlProps> = ({
  label,
  hint,
  htmlFor,
  className,
  labelHidden = false,
  children,
}) => (
  <div className={cn('flex min-w-0 flex-col gap-2', className)}>
    <label
      htmlFor={htmlFor}
      className={cn(
        'type-label-small text-foreground-muted',
        labelHidden && 'sr-only',
      )}
    >
      {label}
    </label>
    {children}
    {hint && <p className="type-meta text-foreground-subtle">{hint}</p>}
  </div>
)

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean
}

/** Quiet interface-layer field. Brand luminosity is reserved for focus. */
export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ className, invalid = false, ...props }, ref) => (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        'h-11 w-full rounded-control border bg-surface/20 px-3 type-body text-foreground outline-none',
        'border-outline/40 placeholder:text-foreground-subtle',
        'transition-colors duration-feedback',
        'hover:border-outline/55 hover:bg-surface/30',
        'focus:border-brand/50 focus:bg-surface/25',
        'focus-visible:ring-2 focus-visible:ring-brand/50 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas',
        'disabled:cursor-not-allowed disabled:opacity-50',
        invalid &&
          'border-status-danger/60 hover:border-status-danger/70 focus:border-status-danger focus-visible:ring-status-danger/35',
        className,
      )}
      {...props}
    />
  ),
)
Input.displayName = 'Input'

export interface SearchFieldProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'type' | 'onChange'> {
  label: string
  labelHidden?: boolean
  value: string
  onChange: (value: string) => void
  onClear?: () => void
  hint?: string
  inputRef?: React.Ref<HTMLInputElement>
}

export const SearchField: React.FC<SearchFieldProps> = ({
  label,
  labelHidden = false,
  value,
  onChange,
  onClear,
  hint,
  inputRef,
  id: providedId,
  className,
  ...props
}) => {
  const generatedId = useId()
  const id = providedId ?? generatedId

  return (
    <FieldControl label={label} htmlFor={id} hint={hint} labelHidden={labelHidden}>
      <div className="relative">
        <MagnifyingGlassIcon
          size={16}
          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-foreground-subtle"
          aria-hidden
        />
        <Input
          ref={inputRef}
          {...props}
          id={id}
          type="search"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className={cn(
            'pl-10 [&::-webkit-search-cancel-button]:appearance-none [&::-webkit-search-decoration]:appearance-none',
            (onClear || value) && 'pr-11',
            className,
          )}
        />
        {value && (
          <button
            type="button"
            aria-label={`Clear ${label.toLowerCase()}`}
            onClick={onClear ?? (() => onChange(''))}
            className="absolute right-0.5 top-1/2 flex h-10 w-10 -translate-y-1/2 items-center justify-center rounded-full text-foreground-subtle transition-colors duration-feedback hover:bg-surface/30 hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/60"
          >
            <XIcon size={14} />
          </button>
        )}
      </div>
    </FieldControl>
  )
}
