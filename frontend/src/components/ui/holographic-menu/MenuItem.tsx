import React from 'react';
import { cn } from '../../../utils/cn';
import { MenuContext } from './context';

export type MenuItemTone = 'primary' | 'secondary' | 'danger' | 'neutral';

const menuItemToneStyles: Record<MenuItemTone, {
  gradient: string;
  rail: string;
  icon: string;
  text: string;
  selectedText: string;
}> = {
  primary: {
    gradient: 'bg-gradient-to-l from-brand/14 via-brand/6 to-transparent',
    rail: 'bg-brand shadow-glow-brand-tight',
    icon: 'group-hover:text-brand group-hover:drop-shadow-glow-brand-tight',
    text: 'text-foreground-muted hover:text-brand',
    selectedText: 'text-foreground',
  },
  secondary: {
    gradient: 'bg-gradient-to-l from-status-success/14 via-status-success/6 to-transparent',
    rail: 'bg-status-success shadow-glow-success',
    icon: 'group-hover:text-status-success group-hover:drop-shadow-glow-output',
    text: 'text-foreground-muted hover:text-status-success',
    selectedText: 'text-foreground',
  },
  danger: {
    gradient: 'bg-gradient-to-l from-status-danger/14 via-status-danger/6 to-transparent',
    rail: 'bg-status-danger shadow-[0_0_10px_currentColor]',
    icon: 'group-hover:text-status-danger',
    text: 'text-status-danger',
    selectedText: 'text-status-danger',
  },
  neutral: {
    gradient: 'bg-gradient-to-l from-surface-highlight/12 via-surface-highlight/5 to-transparent',
    rail: 'bg-surface-highlight',
    icon: 'group-hover:text-surface-highlight',
    text: 'text-foreground-muted hover:text-surface-highlight',
    selectedText: 'text-foreground',
  },
};

export interface MenuItemProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  icon?: React.ReactNode;
  tone?: MenuItemTone;
  selectedTone?: MenuItemTone;
  selected?: boolean;
  closeOnClick?: boolean;
  label?: React.ReactNode;
  description?: React.ReactNode;
  trailing?: React.ReactNode;
}

export const MenuItem = React.forwardRef<HTMLButtonElement, MenuItemProps>(({
  icon,
  children,
  className,
  onClick,
  tone = 'primary',
  selectedTone,
  selected,
  closeOnClick = true,
  label,
  description,
  trailing,
  type = 'button',
  ...props
}, ref) => {
  const { onClose } = React.useContext(MenuContext);
  const toneStyles = menuItemToneStyles[tone];
  const selectedToneStyles = menuItemToneStyles[selectedTone ?? tone];
  const content = label ?? children;

  const handleClick = (e: React.MouseEvent<HTMLButtonElement>) => {
    if (props.disabled) return;
    if (onClick) onClick(e);
    if (closeOnClick) onClose();
  };

  return (
    <button
      ref={ref}
      type={type}
      data-tone={tone}
      data-selected-tone={selectedTone ?? tone}
      data-selected={selected ? '' : undefined}
      className={cn(
        'group relative isolate flex min-h-10 w-full min-w-0 items-center gap-2 overflow-hidden rounded-control px-2 py-2 text-left type-body transition-colors duration-transition active:duration-75 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas',
        selected ? selectedToneStyles.selectedText : toneStyles.text,
        props.disabled && 'opacity-40 cursor-not-allowed active:translate-y-0',
        className,
      )}
      onClick={handleClick}
      {...props}
    >
      <span
        aria-hidden="true"
        className={cn(
          'absolute inset-0 rounded-control opacity-0 transition-opacity duration-transition',
          selected ? selectedToneStyles.gradient : toneStyles.gradient,
          'group-hover:opacity-100 group-active:opacity-100',
          selected && 'opacity-100',
        )}
      />
      <span
        aria-hidden="true"
        className={cn(
          'absolute bottom-2 left-1 top-2 w-px rounded-full opacity-0 transition-opacity duration-transition',
          selected ? selectedToneStyles.rail : toneStyles.rail,
          'group-hover:opacity-80 group-active:opacity-100',
          selected && 'opacity-100',
        )}
      />

      {icon && (
        <div
          className={cn(
            'relative z-10 flex h-4 w-4 shrink-0 items-center justify-center text-foreground-subtle transition-all duration-transition',
            toneStyles.icon,
          )}
        >
          {icon}
        </div>
      )}

      <span className="relative z-10 block min-w-0 flex-1">
        <span className="block truncate type-body tracking-wide">{content}</span>
        {description && (
          <span className="mt-0.5 block truncate type-meta text-foreground-subtle">
            {description}
          </span>
        )}
      </span>

      {trailing && (
        <span className="relative z-10 shrink-0 type-meta text-foreground-subtle">
          {trailing}
        </span>
      )}
    </button>
  );
});

MenuItem.displayName = 'MenuItem';
