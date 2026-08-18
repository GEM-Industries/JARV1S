import React from 'react';
import type { MarkdownSection } from './types';
import { MarkdownContent } from './MarkdownContent';

export const MarkdownSectionView: React.FC<{ section: MarkdownSection }> = ({ section }) => (
  <MarkdownContent content={section.content} />
);
