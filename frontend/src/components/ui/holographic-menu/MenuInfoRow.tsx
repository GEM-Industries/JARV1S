import React from 'react';
import { StatusDot } from '../StatusDot';

type MenuDotStatus = 'success' | 'active' | 'error' | 'warning' | 'neutral' | 'off';

const menuStatusMap: Record<string, MenuDotStatus> = {
  success: 'success',
  warning: 'warning',
  error: 'error',
  neutral: 'neutral',
  active: 'active',
  off: 'off',
};

interface MenuInfoRowProps {
  label: string;
  value: React.ReactNode;
  icon?: React.ReactNode;
  statusIndicator?: string;
}

export const MenuInfoRow: React.FC<MenuInfoRowProps> = ({ label, value, icon, statusIndicator }) => (
  <div className="flex items-center gap-3 px-2 py-2 rounded-control select-none">
    <div className="flex items-center justify-center w-4 h-4 text-foreground-subtle">
      {icon}
    </div>

    <span className="flex-1 type-meta text-foreground-muted opacity-80">
      {label}
    </span>

    <div className="flex items-center gap-2">
      {statusIndicator && (
        <StatusDot status={menuStatusMap[statusIndicator] ?? 'neutral'} />
      )}
      <span className="type-meta font-medium tabular-nums text-foreground">
        {value}
      </span>
    </div>
  </div>
);
