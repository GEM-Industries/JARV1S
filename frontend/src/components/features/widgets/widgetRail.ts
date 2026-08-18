import type { UIEnvelope } from '../../../types';

export interface ReceiptAction {
  type: string;
  widget_id?: string;
  task_id?: string;
}

export const isReceiptRailWidget = (widget: UIEnvelope): boolean =>
  !widget.pinned
  && widget.component === 'ContentWidget'
  && widget.data?.display === 'receipt';

export const isPassiveDetailWidget = (widget: UIEnvelope): boolean =>
  !widget.pinned
  && widget.component === 'BackgroundTaskWidget';

export const getReceiptAction = (widget: UIEnvelope): ReceiptAction | null => {
  const action = widget.data?.action;
  if (!action || typeof action !== 'object') return null;

  const record = action as Record<string, unknown>;
  if (typeof record.type !== 'string' || !record.type) return null;

  return {
    type: record.type,
    widget_id: typeof record.widget_id === 'string' ? record.widget_id : undefined,
    task_id: typeof record.task_id === 'string' ? record.task_id : undefined,
  };
};

export const receiptRailPriority = (widget: UIEnvelope): number => {
  if (widget.data?.receipt_kind !== 'task_progress') return 0;
  if (widget.data?.attention === 'approval') return 3;
  if (widget.data?.status === 'running') return 2;
  return 1;
};
