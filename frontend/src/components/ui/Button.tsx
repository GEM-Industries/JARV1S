import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "../../utils/cn"

const buttonVariants = cva(
  "group relative inline-flex items-center justify-center font-body font-medium overflow-visible outline-none transition-[color,background-color,transform,opacity] duration-feedback ease-hologram active:scale-[0.98] focus-visible:ring-2 focus-visible:ring-brand/70 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas disabled:cursor-not-allowed disabled:opacity-50 motion-reduce:transition-none motion-reduce:active:scale-100",
  {
    variants: {
      variant: {
        default: "",
        ghost: "",
      },
      color: {
        brand: "text-brand bg-brand/10",
        warning: "text-status-warning bg-status-warning/10",
        critical: "text-status-danger-fg bg-status-danger/10",
        subtle: "text-foreground-muted bg-surface/10 hover:text-foreground hover:bg-surface/20",
        neutral: "text-foreground-subtle bg-transparent hover:text-foreground hover:bg-surface/10",
        action: "text-foreground-subtle bg-transparent hover:text-brand",
        danger: "text-foreground-subtle bg-transparent hover:text-status-danger",
      },
      size: {
        default: "h-11 px-8 text-label",
        xs: "min-h-10 h-10 px-3 text-label-small",
        sm: "h-10 px-4 text-label-small",
        md: "h-11 px-6 text-label",
        lg: "h-12 px-10 text-label",
        icon: "h-10 w-10",
        'icon-sm': "h-10 w-10",
      },
      shape: {
        pill: "rounded-full",
        control: "rounded-control",
      },
    },
    compoundVariants: [
      { variant: 'ghost', class: 'bg-transparent' },
    ],
    defaultVariants: {
      variant: "default",
      color: "brand",
      size: "default",
      shape: "pill",
    },
  }
)

const FuiDivider = () => (
  <div className="w-px h-4 flex flex-col opacity-50" aria-hidden="true">
    <div className="h-[4px] bg-current opacity-80" />
    <div className="flex-1 bg-current opacity-30" />
  </div>
)

export interface ButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "color">,
    VariantProps<typeof buttonVariants> {
  icon?: React.ReactNode
  iconPosition?: 'start' | 'end'
}

const ghostChrome: Record<NonNullable<ButtonProps['color']>, string> = {
  brand: 'border-brand/55 bg-brand/10 group-hover:border-brand/80 group-hover:bg-brand/15',
  warning: 'border-status-warning/55 bg-status-warning/10 group-hover:border-status-warning/80 group-hover:bg-status-warning/15',
  critical: 'border-status-danger/45 bg-status-danger/5 group-hover:border-status-danger/70 group-hover:bg-status-danger/10',
  subtle: 'border-outline/45 bg-surface/10 group-hover:border-outline/60 group-hover:bg-surface/20',
  neutral: 'border-outline/25 bg-transparent group-hover:border-outline/45 group-hover:bg-surface/10',
  action: 'border-outline/45 bg-surface/5 group-hover:border-brand/50 group-hover:bg-brand/10',
  danger: 'border-outline/45 bg-surface/5 group-hover:border-status-danger/55 group-hover:bg-status-danger/10',
}

/** Accent colors that use holographic outer + inner ring chrome. */
const accentChrome = {
  brand: {
    border: 'border-brand/70',
    fill: 'bg-brand group-hover:opacity-[0.18] group-active:opacity-25',
    ring: 'border-brand/35 group-hover:border-brand/45 group-active:border-brand/60',
  },
  warning: {
    border: 'border-status-warning/70',
    fill: 'bg-status-warning group-hover:opacity-[0.18] group-active:opacity-25',
    ring: 'border-status-warning/35 group-hover:border-status-warning/45 group-active:border-status-warning/60',
  },
} as const

type AccentColor = keyof typeof accentChrome

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, color, size, shape, children, icon, iconPosition = 'start', type = 'button', ...props }, ref) => {
    const isGhost = variant === 'ghost'
    const resolvedColor = color ?? 'brand'
    const accent = resolvedColor in accentChrome ? accentChrome[resolvedColor as AccentColor] : null
    const showIconDivider = icon && children && !isGhost
    const chromeShape = shape === 'control' ? 'rounded-control' : 'rounded-full'

    return (
      <button
        className={cn(buttonVariants({ variant, color, size, shape, className }))}
        ref={ref}
        type={type}
        data-variant={variant ?? 'default'}
        data-color={color ?? 'brand'}
        data-size={size ?? 'default'}
        data-shape={shape ?? 'pill'}
        {...props}
      >
        {isGhost ? (
          <span
            className={cn(
              'absolute inset-0 border transition-colors duration-feedback ease-hologram motion-reduce:transition-none',
              chromeShape,
              ghostChrome[resolvedColor],
            )}
          />
        ) : (
          <>
            <span
              className={cn(
                'absolute inset-0 border transition-colors duration-feedback ease-hologram motion-reduce:transition-none',
                chromeShape,
                accent ? accent.border : 'border-outline/35 group-hover:border-outline/55',
              )}
            />
            {accent && (
              <>
                <span
                  className={cn(
                    'absolute inset-0 opacity-0 transition-opacity duration-feedback ease-hologram motion-reduce:transition-none',
                    chromeShape,
                    accent.fill,
                  )}
                  style={{
                    maskImage: 'linear-gradient(to top, black, transparent)',
                    WebkitMaskImage: 'linear-gradient(to top, black, transparent)',
                  }}
                />
                <span
                  className={cn(
                    'absolute inset-[3px] border transition-[inset,border-color] duration-feedback ease-hologram',
                    chromeShape,
                    'motion-reduce:transition-none',
                    accent.ring,
                    'group-hover:inset-[4px] group-active:inset-[5px]',
                  )}
                />
              </>
            )}
          </>
        )}

        <span className="relative z-10 flex min-w-0 items-center justify-center gap-3">
          {icon && iconPosition === 'start' && <span className="flex items-center justify-center">{icon}</span>}
          {showIconDivider && iconPosition === 'start' && <FuiDivider />}
          {children && (
            <span className="inline-flex min-w-0 items-center justify-center gap-2 whitespace-nowrap">
              {children}
            </span>
          )}
          {showIconDivider && iconPosition === 'end' && <FuiDivider />}
          {icon && iconPosition === 'end' && <span className="flex items-center justify-center">{icon}</span>}
        </span>
      </button>
    )
  }
)
Button.displayName = "Button"

export { Button, buttonVariants }
