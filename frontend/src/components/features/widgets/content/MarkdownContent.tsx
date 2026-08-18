import React from 'react';
import ReactMarkdown from 'react-markdown';
import { cn } from '../../../../utils/cn';

export interface MarkdownContentProps {
  content: string;
  className?: string;
}

export const MarkdownContent: React.FC<MarkdownContentProps> = ({ content, className }) => (
  <div className={cn('prose prose-invert prose-sm max-w-none', className)}>
    <ReactMarkdown>{content}</ReactMarkdown>
  </div>
);
