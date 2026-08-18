import { cn } from '../../utils/cn';

export interface DividerProps {
  variant?: 'tactical' | 'accented' | 'simple';
  className?: string;
}

export const Divider: React.FC<DividerProps> = ({
  variant = 'tactical',
  className,
}) => (
  <div className={cn('flex items-center', className)}>
    {variant === 'tactical' && (
      <>
        <div className="w-1 h-px bg-outline" />
        <div className="flex-1 h-px bg-outline/20" />
        <div className="w-1 h-px bg-outline" />
      </>
    )}
    {variant === 'accented' && (
      <>
        <div className="w-1.5 h-px bg-outline" />
        <div className="flex-1 h-px bg-outline/30" />
      </>
    )}
    {variant === 'simple' && (
      <div className="w-12 h-px bg-outline/30" />
    )}
  </div>
);
