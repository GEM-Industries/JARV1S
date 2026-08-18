import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '../../utils/cn';

const statusDotVariants = cva(
  'rounded-full flex-shrink-0',
  {
    variants: {
      status: {
        success: 'bg-status-success shadow-glow-success',
        active: 'bg-brand shadow-glow-brand-tight',
        error: 'bg-status-danger',
        warning: 'bg-status-warning',
        neutral: 'bg-foreground-disabled',
        off: 'bg-foreground-disabled/40',
      },
      size: {
        sm: 'w-1 h-1',
        md: 'w-1.5 h-1.5',
        lg: 'w-2.5 h-2.5',
      },
    },
    defaultVariants: {
      status: 'off',
      size: 'md',
    },
  },
);

export type StatusDotStatus = NonNullable<VariantProps<typeof statusDotVariants>['status']>

export interface StatusDotProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, 'color'>,
    VariantProps<typeof statusDotVariants> {}

export const StatusDot: React.FC<StatusDotProps> = ({
  status,
  size,
  className,
  ...props
}) => (
  <div
    aria-hidden
    data-status={status ?? 'off'}
    className={cn(statusDotVariants({ status, size }), className)}
    {...props}
  />
);
