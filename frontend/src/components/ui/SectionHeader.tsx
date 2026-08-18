import { cn } from '../../utils/cn';

export interface SectionHeaderProps {
  icon?: React.ReactNode;
  label: string;
  count?: number;
  className?: string;
}

export const SectionHeader: React.FC<SectionHeaderProps> = ({
  icon,
  label,
  count,
  className,
}) => (
  <div className={cn('flex items-center gap-2 px-4 pb-2 pt-4', className)}>
    {icon}
    <span className="type-label text-foreground-subtle">
      {label}
    </span>
    <div className="flex-1 h-px bg-outline/20" />
    {count !== undefined && (
      <span className="type-meta tabular-nums text-foreground-subtle">{count}</span>
    )}
  </div>
);
