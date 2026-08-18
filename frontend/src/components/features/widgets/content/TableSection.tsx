import React from 'react';
import { cn } from '../../../../utils/cn';
import type { TableSection } from './types';
import { WidgetEyebrow } from '../primitives';

export const TableSectionView: React.FC<{ section: TableSection }> = ({ section }) => (
  <div className="scrollbar-thin overflow-x-auto rounded-control border border-surface-highlight/10">
    <table className="w-full text-sm">
      <thead className="bg-surface/30">
        <tr>
          {section.headers.map((h, i) => (
            <th key={i} className="border-b border-surface-highlight/10 px-3 py-2 text-left">
              <WidgetEyebrow>{h}</WidgetEyebrow>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {section.rows.map((row, ri) => (
          <tr
            key={ri}
            className="border-b border-surface-highlight/[0.06] transition-colors duration-150 last:border-0 hover:bg-surface-highlight/[0.04]"
          >
            {row.map((cell, ci) => (
              <td
                key={ci}
                className={cn(
                  'px-3 py-2.5 font-body text-sm leading-snug',
                  ci === 0 ? 'font-medium text-foreground' : 'text-foreground-muted',
                )}
              >
                {cell}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);
