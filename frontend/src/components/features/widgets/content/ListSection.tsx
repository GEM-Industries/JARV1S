import React from 'react';
import type { ListSection } from './types';
import { WidgetEyebrow } from '../primitives';

export const ListSectionView: React.FC<{ section: ListSection }> = ({ section }) => (
  <ul className="space-y-1">
    {section.items.map((item, i) => (
      <li key={i} className="flex items-start gap-2.5 py-0.5">
        {section.ordered ? (
          <WidgetEyebrow className="mt-[3px] w-4 text-right text-[10px]">{i + 1}.</WidgetEyebrow>
        ) : (
          <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-brand/50" />
        )}
        <span className="font-body text-sm leading-snug text-foreground-muted">{item}</span>
      </li>
    ))}
  </ul>
);
