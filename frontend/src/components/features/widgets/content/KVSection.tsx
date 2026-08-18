import React from 'react';
import type { KVSection } from './types';
import { WidgetEyebrow } from '../primitives';

export const KVSectionView: React.FC<{ section: KVSection }> = ({ section }) => (
  <dl className="space-y-0">
    {Object.entries(section.pairs).map(([key, value]) => (
      <div
        key={key}
        className="flex items-start gap-4 border-b border-surface-highlight/[0.08] py-2.5 last:border-0"
      >
        <dt className="w-[4.5rem] shrink-0 pt-[2px] leading-tight">
          <WidgetEyebrow>{key}</WidgetEyebrow>
        </dt>
        <dd className="min-w-0 flex-1 font-body text-sm leading-snug text-foreground">{value}</dd>
      </div>
    ))}
  </dl>
);
