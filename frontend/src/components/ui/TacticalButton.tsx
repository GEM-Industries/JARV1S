import React, { useState, useEffect } from 'react';
import { cn } from '../../utils/cn';
import { HolographicBorder } from './HolographicBorder';

export interface TacticalButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  active?: boolean;
  label?: string; // Optional label for tooltip/glitch effect (handled by parent if needed)
  onHoverChange?: (label: string | null) => void;
  radius?: string; // Optional custom radius
}

export const TacticalButton = React.forwardRef<HTMLButtonElement, TacticalButtonProps>(({
  className,
  active,
  children,
  label,
  onHoverChange,
  disabled,
  type = 'button',
  ...props
}, forwardedRef) => {
  const localRef = React.useRef<HTMLButtonElement>(null);
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });

  const setRefs = React.useCallback(
    (node: HTMLButtonElement | null) => {
      localRef.current = node;
      if (typeof forwardedRef === 'function') {
        forwardedRef(node);
      } else if (forwardedRef) {
        forwardedRef.current = node;
      }
    },
    [forwardedRef],
  );

  useEffect(() => {
    if (!localRef.current) return;

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        if (entry.borderBoxSize) {
          setDimensions({
            width: entry.borderBoxSize[0].inlineSize,
            height: entry.borderBoxSize[0].blockSize
          });
        } else {
          setDimensions({
            width: entry.contentRect.width,
            height: entry.contentRect.height
          });
        }
      }
    });

    observer.observe(localRef.current);
    return () => observer.disconnect();
  }, []);

  // Also measure on mount just in case
  useEffect(() => {
     if (localRef.current) {
         setDimensions({
             width: localRef.current.offsetWidth,
             height: localRef.current.offsetHeight
         });
     }
  }, []);

  const handleMouseEnter = () => onHoverChange && label && onHoverChange(label);
  const handleMouseLeave = () => onHoverChange && onHoverChange(null);

  return (
    <button
      ref={setRefs}
      type={type}
      disabled={disabled}
      aria-label={props['aria-label'] ?? label}
      aria-pressed={active}
      data-active={active ? '' : undefined}
      data-disabled={disabled ? '' : undefined}
      onMouseEnter={disabled ? undefined : handleMouseEnter}
      onMouseLeave={disabled ? undefined : handleMouseLeave}
      className={cn(
        // Layout & Shape — enforce 40px desktop minimum; callers may grow, not shrink
        "group/tib relative flex min-h-10 min-w-10 items-center justify-center p-2 rounded-full transition-all duration-transition",
        "focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas",
        "disabled:cursor-not-allowed disabled:opacity-50 disabled:pointer-events-none",

        // Default Colors
        "text-foreground-muted",

        // Active State (Persistent): White Icon
        active && "text-foreground",

        className
      )}
      {...props}
    >
      {/* 1. Holographic Border*/}
      <div className={cn(
          "absolute inset-0 transition-all duration-transition pointer-events-none",
          // Base State: Scale 90, Opacity 0
          "opacity-0 scale-90",
          // Hover State: Scale 100, Opacity 100 (Only when not active)
          !active && "group-hover/tib:opacity-100 group-hover/tib:scale-100",
          // Pressed State: Scale 95, Opacity 100 (Only when not active)
          !active && "group-active/tib:scale-95 group-active/tib:opacity-100",
          // Transition settings
          "ease-hologram"
      )}>
          <HolographicBorder
            width={dimensions.width}
            height={dimensions.height}
            color="stroke-brand/40 group-active/tib:stroke-brand transition-colors duration-transition"
          />
      </div>

      {/* 2. Active State Indicator (Rendered only when ACTIVE) */}
      <div
        className={cn(
            "absolute inset-0 transition-all duration-400 ease-hologram",
            active ? "opacity-100 scale-100" : "opacity-0 scale-90"
        )}
      >
          {/* Border Layer (Masked) */}
          <div
              className={cn(
                  "absolute inset-0 rounded-full border border-brand",
                  "[mask-image:conic-gradient(from_180deg,transparent_0deg_30deg,black_30deg_330deg,transparent_330deg_360deg)]"
              )}
          />
          <div
              className="absolute inset-0 rounded-full"
              style={{
                  background: 'linear-gradient(to bottom, oklch(var(--color-brand) / 0.4), oklch(var(--color-brand) / 0))'
              }}
          />
      </div>

      {/* Active Indicator Dot (Bottom) */}
      <div className={cn(
          "absolute -bottom-0 left-1/2 -translate-x-1/2 w-[2px] h-[2px] bg-brand rounded-full shadow-glow-brand-tight transition-all duration-transition",
          active ? "opacity-100 scale-100" : "opacity-0 scale-0"
      )} />

      {/* Content */}
      <div className={cn(
        "relative z-10 transition-all duration-transition",
        "group-hover/tib:text-brand",
        // Click State: Scale down content too for unified feel, use Brand Primary for energy surge
        !active && "group-active/tib:scale-95 group-active/tib:text-brand group-active/tib:drop-shadow-glow-brand-intense",
        // Active: White text overrides hover color
        active && "text-foreground group-hover/tib:text-foreground"
      )}>
          {children}
      </div>
    </button>
  );
});

TacticalButton.displayName = 'TacticalButton';
