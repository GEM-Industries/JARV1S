import React, { useMemo } from 'react'
import { CaretDownIcon } from '@phosphor-icons/react'
import { Select as BaseSelect } from '@base-ui/react/select'
import { cn } from '../../utils/cn'

export interface SelectOption {
  value: string
  label: string
  description?: string
  disabled?: boolean
  /** Optional group heading; options with the same group render together */
  group?: string
}

export interface SelectProps {
  value: string
  onChange: (value: string) => void
  options: SelectOption[]
  placeholder?: string
  disabled?: boolean
  id?: string
  /** Layout-only overrides (width, grid placement). Avoid color/border overrides. */
  className?: string
  'aria-label'?: string
  'aria-labelledby'?: string
}

function renderOption(option: SelectOption) {
  return (
    <BaseSelect.Item
      key={option.value || option.label}
      value={option.value}
      label={option.label}
      disabled={option.disabled}
      className={cn(
        'group/item relative min-h-10 w-full cursor-default rounded-control px-3 py-2 text-left outline-none',
        'type-body text-foreground-muted transition-colors duration-feedback',
        'hover:bg-surface/25 hover:text-foreground',
        'data-[highlighted]:bg-surface/25 data-[highlighted]:text-foreground',
        'data-[selected]:bg-brand/12 data-[selected]:text-foreground',
        'data-[disabled]:cursor-not-allowed data-[disabled]:opacity-40',
      )}
    >
      <span
        aria-hidden
        className="absolute bottom-2 left-0 top-2 w-px rounded-full bg-brand opacity-0 group-data-[selected]/item:opacity-100"
      />
      <BaseSelect.ItemText className="block truncate">{option.label}</BaseSelect.ItemText>
      {option.description && (
        <span className="mt-0.5 block truncate type-meta text-foreground-subtle">
          {option.description}
        </span>
      )}
    </BaseSelect.Item>
  )
}

/**
 * Quiet single-select for forms and settings.
 * Closed trigger matches Input; open list matches trigger width and surface.
 */
export const Select: React.FC<SelectProps> = ({
  value,
  onChange,
  options,
  placeholder = 'Select…',
  disabled = false,
  id,
  className,
  'aria-label': ariaLabel,
  'aria-labelledby': ariaLabelledby,
}) => {
  const labels = useMemo(
    () => Object.fromEntries(options.map((option) => [option.value, option.label])),
    [options],
  )

  const grouped = useMemo(() => {
    const hasGroups = options.some((option) => option.group)
    if (!hasGroups) return null
    const map = new Map<string, SelectOption[]>()
    for (const option of options) {
      const key = option.group ?? 'Other'
      const bucket = map.get(key)
      if (bucket) bucket.push(option)
      else map.set(key, [option])
    }
    return Array.from(map.entries())
  }, [options])

  return (
    <BaseSelect.Root
      value={value}
      onValueChange={(next) => {
        if (typeof next === 'string') onChange(next)
      }}
      disabled={disabled}
      id={id}
      highlightItemOnHover={false}
    >
      <BaseSelect.Trigger
        aria-label={ariaLabel}
        aria-labelledby={ariaLabelledby}
        className={cn(
          'group flex h-11 min-h-11 w-full min-w-0 items-center justify-between gap-2 rounded-control border bg-surface/20 px-3 text-left',
          'type-body text-foreground outline-none',
          'border-outline/40 transition-colors duration-feedback',
          'hover:border-outline/55 hover:bg-surface/30',
          'focus:border-brand/50 focus:bg-surface/25',
          'data-[popup-open]:border-brand/50 data-[popup-open]:bg-surface/25',
          'disabled:cursor-not-allowed disabled:opacity-50',
          className,
        )}
      >
        <BaseSelect.Value
          placeholder={placeholder}
          className="min-w-0 truncate data-[placeholder]:text-foreground-subtle"
        >
          {(selected) => (typeof selected === 'string' ? (labels[selected] ?? selected) : placeholder)}
        </BaseSelect.Value>
        <BaseSelect.Icon className="flex shrink-0">
          <CaretDownIcon
            size={14}
            weight="bold"
            className="text-foreground-subtle transition-transform duration-feedback ease-hologram group-data-[popup-open]:rotate-180 group-data-[popup-open]:text-brand motion-reduce:transition-none"
          />
        </BaseSelect.Icon>
      </BaseSelect.Trigger>

      <BaseSelect.Portal>
        <BaseSelect.Positioner
          className="z-[80] outline-none"
          side="bottom"
          align="start"
          sideOffset={6}
          alignItemWithTrigger={false}
        >
          <BaseSelect.Popup
            aria-label={ariaLabel}
            aria-labelledby={ariaLabelledby}
            className={cn(
              'w-[var(--anchor-width)] max-h-60 overflow-y-auto outline-none scrollbar-thin',
              'rounded-control border border-outline/40 bg-canvas p-1.5',
              'motion-safe:animate-in motion-safe:fade-in motion-safe:zoom-in-95 motion-safe:duration-instant motion-safe:ease-snappy-in motion-safe:origin-top',
            )}
          >
            {options.length === 0 ? (
              <p className="px-3 py-2 type-body text-foreground-subtle">No options</p>
            ) : (
              <BaseSelect.List className="flex flex-col gap-0.5 outline-none">
                {grouped
                  ? grouped.map(([groupLabel, groupOptions]) => (
                      <BaseSelect.Group key={groupLabel} className="flex flex-col gap-0.5">
                        <BaseSelect.GroupLabel className="px-3 py-1.5 type-meta text-foreground-subtle">
                          {groupLabel}
                        </BaseSelect.GroupLabel>
                        {groupOptions.map(renderOption)}
                      </BaseSelect.Group>
                    ))
                  : options.map(renderOption)}
              </BaseSelect.List>
            )}
          </BaseSelect.Popup>
        </BaseSelect.Positioner>
      </BaseSelect.Portal>
    </BaseSelect.Root>
  )
}
