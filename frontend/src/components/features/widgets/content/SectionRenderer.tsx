import React from 'react';
import { MetricSectionView } from './MetricSection';
import { KVSectionView } from './KVSection';
import { TableSectionView } from './TableSection';
import { ListSectionView } from './ListSection';
import { CodeSectionView } from './CodeSection';
import { MarkdownSectionView } from './MarkdownSection';
import type { ContentSection } from './types';

const RENDERERS: Record<string, React.FC<{ section: ContentSection }>> = {
  markdown: MarkdownSectionView as React.FC<{ section: ContentSection }>,
  table:    TableSectionView    as React.FC<{ section: ContentSection }>,
  list:     ListSectionView     as React.FC<{ section: ContentSection }>,
  code:     CodeSectionView     as React.FC<{ section: ContentSection }>,
  kv:       KVSectionView       as React.FC<{ section: ContentSection }>,
  metric:   MetricSectionView   as React.FC<{ section: ContentSection }>,
};

export const SectionRenderer: React.FC<{ section: ContentSection }> = ({ section }) => {
  const Renderer = RENDERERS[section.type];
  if (!Renderer) return null;
  return <Renderer section={section} />;
};
