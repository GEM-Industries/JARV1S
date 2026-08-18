import React, { useState } from 'react';
import { CopyIcon, CheckIcon } from '@phosphor-icons/react';
import { cn } from '../../../../utils/cn';
import type { CodeSection } from './types';

export const CodeSectionView: React.FC<{ section: CodeSection }> = ({ section }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(section.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="overflow-hidden rounded-control border border-surface-highlight/10 bg-surface-sunken">
      <div className="px-3 py-1.5 border-b border-surface-highlight/10 flex items-center justify-between">
        <span className="font-mono text-[9px] uppercase tracking-widest text-outline">
          {section.language ?? 'code'}
        </span>
        <button
          onClick={handleCopy}
          className={cn(
            'flex items-center gap-1 font-mono text-[9px] uppercase tracking-widest transition-colors duration-200 cursor-pointer',
            copied ? 'text-brand' : 'text-outline hover:text-foreground-muted'
          )}
        >
          {copied ? <CheckIcon size={10} weight="bold" /> : <CopyIcon size={10} />}
          {copied ? 'copied' : 'copy'}
        </button>
      </div>
      <pre className="p-3 overflow-x-auto scrollbar-thin">
        <code className="font-mono text-xs text-brand leading-relaxed whitespace-pre">
          {section.content}
        </code>
      </pre>
    </div>
  );
};
