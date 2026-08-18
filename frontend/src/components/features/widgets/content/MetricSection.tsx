import React from 'react';
import { cn } from '../../../../utils/cn';
import type { MetricItem, MetricSection } from './types';
import { WidgetCard, WidgetEyebrow, WidgetStatusDot } from '../primitives';

const STATUS: Record<string, { bar: string; text: string; dot: 'active' | 'warning' | 'error' }> = {
  good: { bar: 'from-brand to-status-success', text: 'text-brand', dot: 'active' },
  warning: { bar: 'from-status-warning to-status-warning/70', text: 'text-status-warning', dot: 'warning' },
  critical: { bar: 'from-status-danger to-status-danger/70', text: 'text-status-danger', dot: 'error' },
};

const MetricCard: React.FC<{ item: MetricItem }> = ({ item }) => {
  const colors = STATUS[item.status ?? 'good'] ?? STATUS.good;
  const pct = Math.min(100, item.percent ?? 0);

  return (
    <WidgetCard className="flex min-w-0 flex-col gap-2 p-3">
      <div className="flex items-center justify-between gap-2">
        <WidgetEyebrow>{item.label}</WidgetEyebrow>
        <WidgetStatusDot status={colors.dot} />
      </div>

      <span className={cn('font-display text-xl font-medium tabular-nums leading-none', colors.text)}>
        {item.value}
      </span>

      {item.percent != null && (
        <div className="h-[3px] overflow-hidden rounded-full bg-surface-sunken">
          <div
            className={cn(
              'h-full rounded-full bg-gradient-to-r transition-all duration-700 ease-hologram',
              colors.bar,
            )}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}

      {item.sublabel && (
        <span className="type-meta text-foreground-subtle">{item.sublabel}</span>
      )}
    </WidgetCard>
  );
};

export const MetricSectionView: React.FC<{ section: MetricSection }> = ({ section }) => (
  <div className="grid grid-cols-2 gap-2">
    {section.items.map((item, i) => (
      <MetricCard key={i} item={item} />
    ))}
  </div>
);
