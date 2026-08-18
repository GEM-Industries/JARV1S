import React from 'react';
import { cn } from '../../../utils/cn';

interface CompressedWidgetViewProps {
  icon?: React.ReactNode;
  label: React.ReactNode;
  labelVariant?: 'display' | 'mono';
  eyebrow?: string;
  subLabel?: string;
  layout?: 'stack' | 'row';
  variant?: 'default' | 'receipt';
  indicator?: 'running' | 'warning' | 'success' | 'error';
}

export const CompressedWidgetView: React.FC<CompressedWidgetViewProps> = ({ 
  icon, 
  label, 
  labelVariant = 'display',
  eyebrow,
  subLabel,
  layout = 'stack',
  variant = 'default',
  indicator,
}) => {
  const indicatorClass = indicator === 'running'
    ? 'border-brand bg-brand/70 shadow-[0_0_6px_oklch(var(--color-brand))] animate-pulse'
    : indicator === 'warning'
      ? 'border-status-warning bg-status-warning/80'
      : indicator === 'success'
        ? 'border-status-success bg-status-success/70'
        : indicator === 'error'
          ? 'border-status-danger bg-status-danger/80'
          : null;

  if (variant === 'receipt') {
    return (
      <div className="flex h-full w-full items-center px-5 py-4">
        <div className="min-w-0 flex flex-1 flex-col justify-center gap-1.5">
          {eyebrow && (
            <span className="font-mono text-[10px] uppercase tracking-[0.22em] text-brand/80 truncate">
              {eyebrow}
            </span>
          )}
          <span
            className="font-display text-lg leading-[1.1] text-foreground tracking-tight overflow-hidden"
            style={{
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
            }}
          >
            {label}
          </span>
          {subLabel && (
            <span className="text-xs font-body text-foreground-muted leading-none truncate">
              {subLabel}
            </span>
          )}
        </div>
        {indicatorClass && (
          <span className={cn('ml-3 h-2 w-2 shrink-0 rounded-full border', indicatorClass)} />
        )}
      </div>
    );
  }

  if (layout === 'row') {
    return (
      <div className="flex items-center gap-3 px-1 h-full w-full">
        {icon && (
          <div className="flex items-center justify-center shrink-0">
            {icon}
          </div>
        )}
        <div className="flex flex-col items-start justify-center">
          <span className={cn(
            "leading-none text-foreground tracking-tight",
            labelVariant === 'display' ? "font-display font-medium text-lg" : "font-mono font-bold text-sm"
          )}>
            {label}
          </span>
          {subLabel && (
            <span className="text-[10px] font-body text-foreground-muted uppercase tracking-wider -mt-0.5">
              {subLabel}
            </span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center h-full w-full">
      {icon && (
        <div className="flex items-center justify-center -mb-1">
          {icon}
        </div>
      )}
      <span className={cn(
        "leading-none text-foreground tracking-tighter",
        labelVariant === 'display' ? "font-display font-medium text-base" : "font-mono font-bold text-[10px] opacity-80"
      )}>
        {label}
      </span>
    </div>
  );
};
