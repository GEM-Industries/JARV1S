import React, { useState } from 'react';
import {
  CheckCircleIcon,
  XCircleIcon,
  ShieldWarningIcon,
  CaretDownIcon,
  CaretUpIcon,
} from '@phosphor-icons/react';
import { cn } from '../../../utils/cn';
import { Button } from '../../ui/Button';
import { WidgetDefinition, BaseWidgetProps } from './types';
import { jarvisClient } from '../../../client/JarvisClient';

interface PendingInputData {
  input_id: string;
  kind: 'approval' | string;
  status: 'pending' | 'approved' | 'denied' | 'expired' | 'cancelled' | string;
  prompt: string;
  detail?: string | null;
  risk?: 'low' | 'medium' | 'high' | string | null;
  result?: string | null;
  source?: Record<string, unknown>;
}

const statusLabel = (status: string): string => {
  if (status === 'pending') return 'Pending';
  if (status === 'approved') return 'Approved';
  if (status === 'denied') return 'Denied';
  if (status === 'expired') return 'Expired';
  if (status === 'cancelled') return 'Cancelled';
  return status;
};

const PendingInputHero: React.FC<PendingInputData & BaseWidgetProps> = ({
  input_id,
  status,
  prompt,
  detail,
  risk,
  result,
}) => {
  const [showDetail, setShowDetail] = useState(false);
  const [acting, setActing] = useState(false);
  const isPending = status === 'pending';
  const isBad = status === 'denied' || status === 'expired' || status === 'cancelled';

  const resolve = (decision: 'approve' | 'deny') => {
    setActing(true);
    jarvisClient.sendMessage('ui.action', {
      plugin: 'system',
      tool: 'resolve_pending_input',
      args: { input_id, decision },
    });
  };

  return (
    <div className="flex flex-col h-full px-5 py-5 select-none">
      <div className="flex items-center gap-3 mb-4 shrink-0">
        <ShieldWarningIcon
          size={18}
          weight="fill"
          className={cn(isPending ? 'text-status-warning' : isBad ? 'text-status-danger' : 'text-status-success')}
        />
        <span className="text-sm font-body font-medium uppercase tracking-wider text-foreground">
          Approval Required
        </span>
        <span
          className={cn(
            'ml-auto text-[11px] font-mono uppercase tracking-wide',
            isPending ? 'text-status-warning' : isBad ? 'text-status-danger' : 'text-status-success',
          )}
        >
          {statusLabel(status)}
        </span>
      </div>

      {risk && (
        <div className="mb-2 w-fit rounded-full border border-status-warning/30 bg-status-warning/10 px-2 py-0.5 text-[9px] font-mono uppercase tracking-widest text-status-warning">
          {risk} risk
        </div>
      )}

      <p className="font-body text-lg text-foreground leading-snug mb-3">
        {prompt}
      </p>

      {result && !isPending && (
        <div className="flex-1 mb-3 overflow-auto">
          <pre className="whitespace-pre-wrap break-words rounded-control bg-surface-highlight/10 px-3 py-2 font-mono text-xs text-foreground-muted">
            {result}
          </pre>
        </div>
      )}

      {detail && (
        <>
          <button
            type="button"
            className="flex items-center gap-2 text-outline hover:text-foreground transition-colors mb-3 shrink-0"
            onClick={() => setShowDetail(v => !v)}
          >
            <span className="text-[11px] font-mono tracking-wide">
              {showDetail ? 'hide detail' : 'show detail'}
            </span>
            {showDetail ? <CaretUpIcon size={12} /> : <CaretDownIcon size={12} />}
          </button>

          {showDetail && (
            <div className="mb-3 shrink-0">
              <pre className="whitespace-pre-wrap break-words rounded-control bg-surface-highlight/10 px-3 py-2 font-mono text-xs text-foreground-muted">
                {detail}
              </pre>
            </div>
          )}
        </>
      )}

      {isPending && (
        <div className="flex gap-3 mt-auto shrink-0">
          <Button
            variant="ghost"
            color="brand"
            size="sm"
            onClick={() => resolve('approve')}
            disabled={acting}
            className="flex-1"
            icon={<CheckCircleIcon size={14} weight="fill" />}
          >
            {acting ? 'Running…' : 'Approve'}
          </Button>
          <Button
            variant="ghost"
            color="subtle"
            size="sm"
            onClick={() => resolve('deny')}
            disabled={acting}
            className="flex-1"
            icon={<XCircleIcon size={14} weight="fill" />}
          >
            Deny
          </Button>
        </div>
      )}
    </div>
  );
};

export const PendingInputWidget: WidgetDefinition<PendingInputData> = {
  Hero: PendingInputHero,
  getCompressedConfig: (data) => ({
    icon: (
      <ShieldWarningIcon
        size={20}
        weight="fill"
        className={cn(
          data.status === 'pending'
            ? 'text-status-warning'
            : data.status === 'approved'
              ? 'text-status-success'
              : 'text-status-danger',
        )}
      />
    ),
    label: statusLabel(data.status),
    labelVariant: 'mono',
    width: 'wide',
  }),
};
