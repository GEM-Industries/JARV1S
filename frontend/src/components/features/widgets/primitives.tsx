import React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '../../../utils/cn';
import { StatusDot } from '../../ui/StatusDot';

export const WidgetEyebrow: React.FC<
  React.HTMLAttributes<HTMLSpanElement> & { muted?: boolean; size?: 'sm' | 'md' }
> = ({ className, muted, size = 'sm', children, ...props }) => (
  <span
    className={cn(
      'font-mono uppercase tracking-widest shrink-0',
      size === 'sm' ? 'text-[9px]' : 'text-[10px]',
      muted ? 'text-foreground-muted/70' : 'text-foreground-subtle',
      className,
    )}
    {...props}
  >
    {children}
  </span>
);

export const WidgetMetaPill: React.FC<React.HTMLAttributes<HTMLSpanElement>> = ({
  className,
  children,
  ...props
}) => (
  <span
    className={cn(
      'shrink-0 rounded border border-surface-highlight/15 bg-surface/60 px-1.5 py-0.5',
      'font-mono text-[9px] uppercase tracking-widest text-foreground-subtle',
      className,
    )}
    {...props}
  >
    {children}
  </span>
);

/** Widget-layer alias over the shared StatusDot vocabulary. */
export const WidgetStatusDot: React.FC<
  React.ComponentProps<typeof StatusDot>
> = (props) => <StatusDot size="md" {...props} />;

const panelVariants = cva('rounded-control border p-3', {
  variants: {
    tone: {
      default: 'border-outline/50 bg-surface-sunken/40',
      warning: 'border-status-warning/20 bg-status-warning/[0.04]',
      inset: 'border-surface-highlight/10 bg-surface/25',
    },
  },
  defaultVariants: { tone: 'default' },
});

export interface WidgetPanelProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof panelVariants> {
  title?: string;
  icon?: React.ReactNode;
  action?: React.ReactNode;
}

export const WidgetPanel: React.FC<WidgetPanelProps> = ({
  title,
  icon,
  action,
  tone,
  className,
  children,
  ...props
}) => (
  <div className={cn(panelVariants({ tone }), className)} {...props}>
    {title && (
      <div className="mb-2 flex items-center gap-2">
        {icon && <span className="text-brand/80">{icon}</span>}
        <WidgetEyebrow muted size="md">
          {title}
        </WidgetEyebrow>
        {action && <div className="ml-auto">{action}</div>}
      </div>
    )}
    <div className="space-y-1.5">{children}</div>
  </div>
);

export interface WidgetHeaderProps extends React.HTMLAttributes<HTMLDivElement> {
  title: string;
  subtitle?: string;
  leading?: React.ReactNode;
  trailing?: React.ReactNode;
  meta?: React.ReactNode;
}

export const WidgetHeader: React.FC<WidgetHeaderProps> = ({
  title,
  subtitle,
  leading,
  trailing,
  meta,
  className,
  ...props
}) => (
  <div
    className={cn(
      'flex shrink-0 items-center gap-3 border-b border-surface-highlight/[0.08] px-5 py-3.5',
      className,
    )}
    {...props}
  >
    {leading}
    <div className="min-w-0 flex-1">
      <div className="truncate text-sm font-body font-medium uppercase tracking-wider text-foreground">
        {title}
      </div>
      {subtitle && (
        <div className="mt-0.5 font-mono text-[10px] uppercase tracking-widest text-foreground-muted/55">
          {subtitle}
        </div>
      )}
    </div>
    {trailing}
    {meta}
  </div>
);

export const WidgetBody: React.FC<React.HTMLAttributes<HTMLDivElement>> = ({
  className,
  children,
  ...props
}) => (
  <div className={cn('min-h-0 flex-1 overflow-y-auto scrollbar-thin px-5 py-4', className)} {...props}>
    {children}
  </div>
);

export interface WidgetRowProps extends Omit<React.HTMLAttributes<HTMLDivElement>, 'title'> {
  icon?: React.ReactNode;
  label?: React.ReactNode;
  description?: React.ReactNode;
  compact?: boolean;
  descriptionClassName?: string;
}

export const WidgetRow: React.FC<WidgetRowProps> = ({
  icon,
  label,
  description,
  compact,
  descriptionClassName,
  className,
  children,
  ...props
}) => (
  <div
    className={cn('flex items-start gap-2', compact ? 'text-[11px] text-foreground-muted' : 'text-sm', className)}
    {...props}
  >
    {icon && <span className="mt-0.5 shrink-0">{icon}</span>}
    <div className="min-w-0 flex-1">
      {label != null && <div className="font-mono text-foreground">{label}</div>}
      {description != null && (
        <div className={cn('line-clamp-2 text-foreground-muted', descriptionClassName)}>{description}</div>
      )}
      {children}
    </div>
  </div>
);

export const WidgetCard: React.FC<React.HTMLAttributes<HTMLDivElement>> = ({
  className,
  children,
  ...props
}) => (
  <div
    className={cn('rounded-control border border-surface-highlight/10 bg-surface/20', className)}
    {...props}
  >
    {children}
  </div>
);

export const WidgetSectionStack: React.FC<React.HTMLAttributes<HTMLDivElement>> = ({
  className,
  children,
  ...props
}) => (
  <div className={cn('space-y-5', className)} {...props}>
    {children}
  </div>
);
