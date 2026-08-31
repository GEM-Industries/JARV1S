import React from 'react'
import { DotsThreeVerticalIcon } from '@phosphor-icons/react'
import { Menu } from '@base-ui/react/menu'
import { cn } from '../../utils/cn'

const popupClass = cn(
  'min-w-44 max-w-64 origin-[var(--transform-origin)] overflow-y-auto outline-none scrollbar-thin',
  'rounded-control border border-outline/40 bg-canvas p-1.5',
  'motion-safe:animate-in motion-safe:fade-in motion-safe:zoom-in-95 motion-safe:duration-instant motion-safe:ease-snappy-in',
)

const triggerClass = cn(
  'inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full',
  'text-foreground-subtle transition-colors duration-feedback ease-hologram',
  'hover:bg-surface/20 hover:text-foreground',
  'outline-none',
  'data-[popup-open]:bg-surface/20 data-[popup-open]:text-foreground',
  'disabled:cursor-not-allowed disabled:opacity-50',
  'motion-reduce:transition-none',
)

export interface ActionMenuProps {
  /** Accessible name for the trigger and popup, e.g. "More actions for Bedroom". */
  label: string
  children: React.ReactNode
  disabled?: boolean
}

/**
 * Quiet overflow menu for operational list rows.
 * Keep one labeled `Button` on the row (or none); put remaining actions here.
 */
export const ActionMenu: React.FC<ActionMenuProps> & {
  Item: typeof ActionMenuItem
  Separator: typeof ActionMenuSeparator
} = ({ label, children, disabled = false }) => (
  <Menu.Root highlightItemOnHover={false} modal={false} disabled={disabled}>
    <Menu.Trigger aria-label={label} disabled={disabled} className={triggerClass}>
      <DotsThreeVerticalIcon size={16} weight="bold" />
    </Menu.Trigger>
    <Menu.Portal>
      <Menu.Positioner className="z-[80] outline-none" side="bottom" align="end" sideOffset={6}>
        <Menu.Popup aria-label={label} className={popupClass}>
          <Menu.Viewport className="flex flex-col gap-0.5 outline-none">{children}</Menu.Viewport>
        </Menu.Popup>
      </Menu.Positioner>
    </Menu.Portal>
  </Menu.Root>
)

export interface ActionMenuItemProps {
  children: React.ReactNode
  icon?: React.ReactNode
  tone?: 'neutral' | 'danger'
  disabled?: boolean
  onClick?: () => void
}

const ActionMenuItem: React.FC<ActionMenuItemProps> = ({
  children,
  icon,
  tone = 'neutral',
  disabled = false,
  onClick,
}) => (
  <Menu.Item
    disabled={disabled}
    onClick={onClick}
    className={cn(
      'group/item relative flex min-h-10 w-full cursor-default items-center gap-2 rounded-control px-3 py-2 text-left outline-none',
      'type-body transition-colors duration-feedback',
      'data-[disabled]:cursor-not-allowed data-[disabled]:opacity-40',
      tone === 'danger'
        ? 'text-status-danger hover:bg-status-danger/10 hover:text-status-danger-fg data-[highlighted]:bg-status-danger/10 data-[highlighted]:text-status-danger-fg'
        : 'text-foreground-muted hover:bg-surface/25 hover:text-foreground data-[highlighted]:bg-surface/25 data-[highlighted]:text-foreground',
    )}
  >
    {icon && (
      <span className="flex h-4 w-4 shrink-0 items-center justify-center text-foreground-subtle">
        {icon}
      </span>
    )}
    <span className="min-w-0 flex-1 truncate">{children}</span>
  </Menu.Item>
)

const ActionMenuSeparator: React.FC = () => (
  <Menu.Separator className="my-1 h-px bg-outline/25" />
)

ActionMenu.Item = ActionMenuItem
ActionMenu.Separator = ActionMenuSeparator
ActionMenu.displayName = 'ActionMenu'
