import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "../../utils/cn"

const innerRingBase = "before:absolute before:inset-[3px] before:rounded-[calc(var(--hologram-radius)-4px)] before:[corner-shape:squircle] before:pointer-events-none before:border before:[mask-image:linear-gradient(to_bottom,black,transparent)] before:transition-all before:duration-400 before:ease-hologram"

const hologramVariants = cva(
  "relative w-auto overflow-hidden rounded-[var(--hologram-radius)] transition-all duration-400 ease-hologram",
  {
    variants: {
      // 1. STRUCTURE (Shape/Mask)
      variant: {
        base: cn("border bg-transparent shadow-none [corner-shape:squircle]", innerRingBase),
        ringed: "bg-transparent border [corner-shape:round]",
        corners: "bg-transparent [corner-shape:squircle] before:absolute before:inset-0 before:rounded-[var(--hologram-radius)] before:[corner-shape:squircle] before:border before:[mask-image:conic-gradient(from_45deg,black,transparent_45deg_135deg,black_180deg,transparent_225deg_315deg,black_360deg)]",
      },
      // 2. STATE (Color/Glow)
      color: {
        default: "",
        warning: "",
        error: "",
        inactive: "",
      },
    },
    compoundVariants: [
      // Base Variant Colors
      { 
        variant: "base", 
        color: "default", 
        class: "border-surface-highlight before:border-[oklch(var(--color-surface))]" 
      },
      {
        variant: "base",
        color: "warning",
        class: "border-status-warning/70 before:border-status-warning/40"
      },
      { 
        variant: "base", 
        color: "error", 
        class: "border-hologram-error before:border-[oklch(var(--color-hologram-error-inner))]" 
      },
      { 
        variant: "base", 
        color: "inactive", 
        class: "border-hologram-inactive before:border-[oklch(var(--color-hologram-inactive-inner))]" 
      },

      // Ringed Variant Colors
      { variant: "ringed", color: "default", class: "border-surface-highlight shadow-hologram-inset" },
      { variant: "ringed", color: "warning", class: "border-status-warning/70 shadow-hologram-inset-warning" },
      { variant: "ringed", color: "error", class: "border-hologram-error shadow-hologram-inset-error" },
      { variant: "ringed", color: "inactive", class: "border-hologram-inactive shadow-hologram-inset-inactive" },

      // Corners Variant Colors (Targeting pseudo-elements)
      { variant: "corners", color: "default", class: "before:border-surface-highlight before:shadow-hologram-inset" },
      { variant: "corners", color: "warning", class: "before:border-status-warning/70 before:shadow-hologram-inset-warning" },
      { variant: "corners", color: "error", class: "before:border-hologram-error before:shadow-hologram-inset-error" },
      { variant: "corners", color: "inactive", class: "before:border-hologram-inactive before:shadow-hologram-inset-inactive" },
    ],
    defaultVariants: {
      variant: "base",
      color: "default",
    },
  }
)

export interface HologramProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "color">,
    VariantProps<typeof hologramVariants> {}

const Hologram = React.forwardRef<HTMLDivElement, HologramProps>(
  ({ className, variant, color, style, ...props }, ref) => {
    return (
      <div
        ref={ref}
        style={{ 
          '--hologram-radius': 'var(--radius-shell)',
          ...style 
        } as React.CSSProperties}
        className={cn(hologramVariants({ variant, color, className }))}
        {...props}
      />
    )
  }
)
Hologram.displayName = "Hologram"

export { Hologram, hologramVariants }
