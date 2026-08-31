import React from 'react';
import { ArticleIcon } from '@phosphor-icons/react';
import { WidgetDefinition, BaseWidgetProps } from './types';
import { SectionRenderer } from './content/SectionRenderer';
import type { ContentData } from './content/types';
import { WidgetBody, WidgetHeader, WidgetMetaPill, WidgetSectionStack } from './primitives';

const receiptIndicator = (data: ContentData): 'running' | 'warning' | 'success' | 'error' | undefined => {
  if (data.receipt_kind !== 'task_progress') return undefined;
  if (data.attention === 'approval') return 'warning';
  if (data.status === 'completed') return 'success';
  if (data.status === 'failed') return 'error';
  if (data.status === 'cancelled') return 'warning';
  if (data.status === 'running') return 'running';
  return undefined;
};

export type { ContentData } from './content/types';

const EmptyState: React.FC = () => (
  <div className="flex h-full flex-col items-center justify-center gap-2 py-8 opacity-25">
    <ArticleIcon size={36} weight="light" className="text-outline" />
    <span className="font-mono text-[9px] uppercase tracking-widest text-outline">No content</span>
  </div>
);

const ContentHero: React.FC<ContentData & BaseWidgetProps> = ({ title, sections = [] }) => (
  <div className="flex h-full select-none flex-col overflow-hidden">
    <WidgetHeader
      title={title}
      meta={
        <WidgetMetaPill>
          {sections.length} {sections.length === 1 ? 'section' : 'sections'}
        </WidgetMetaPill>
      }
    />
    <WidgetBody>
      {sections.length > 0 ? (
        <WidgetSectionStack>
          {sections.map((section, i) => (
            <SectionRenderer key={i} section={section} />
          ))}
        </WidgetSectionStack>
      ) : (
        <EmptyState />
      )}
    </WidgetBody>
  </div>
);

export const ContentWidget: WidgetDefinition<ContentData> = {
  Hero: ContentHero,
  getCompressedConfig: (data) => {
    const isReceipt = data.display === 'receipt';
    const label = isReceipt ? (data.line || data.title || 'Receipt') : (data.title || 'Content');
    const subLabel = isReceipt ? (data.sublabel || data.title) : undefined;
    const maxLabelLength = 18;
    return {
      icon: isReceipt ? undefined : <ArticleIcon size={20} weight="light" className="text-brand" />,
      label: !isReceipt && label.length > maxLabelLength ? `${label.slice(0, maxLabelLength)}…` : label,
      labelVariant: 'display' as const,
      eyebrow: isReceipt ? data.title : undefined,
      subLabel,
      variant: isReceipt ? 'receipt' as const : 'default' as const,
      width: 'wide' as const,
      indicator: receiptIndicator(data),
    };
  },
};
