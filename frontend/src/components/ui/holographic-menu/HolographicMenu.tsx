import React, { useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { cn } from '../../../utils/cn';
import { Hologram } from '../Hologram';
import { StatusBarMenuContent } from './StatusBarMenuContent';

type HologramColor = 'default' | 'warning' | 'error' | 'inactive';

export interface HolographicMenuProps {
  children: React.ReactNode;
  onClose: () => void;
  className?: string;
  align?: 'left' | 'right';
  hologramColor?: HologramColor;
  /** Accessible name for the menu */
  'aria-label'?: string;
}

/**
 * StatusBar action menu with holographic chrome.
 * Used by independently anchored menus; shared right-side navigation uses
 * StatusBarSurfaceHost so its shell can persist between destinations.
 */
export const HolographicMenu = React.forwardRef<HTMLDivElement, HolographicMenuProps>(({
  children,
  onClose,
  className,
  align = 'right',
  hologramColor = 'default',
  'aria-label': ariaLabel = 'Menu',
}, forwardedRef) => {
  const [isVisible, setIsVisible] = useState(false);
  const [isExiting, setIsExiting] = useState(false);
  const menuRef = React.useRef<HTMLDivElement>(null);
  const previousFocusRef = React.useRef<HTMLElement | null>(null);
  const closedRef = useRef(false);

  useImperativeHandle(forwardedRef, () => menuRef.current as HTMLDivElement);

  const finishClose = useCallback(() => {
    if (closedRef.current) return;
    closedRef.current = true;
    onClose();
    previousFocusRef.current?.focus();
  }, [onClose]);

  const handleRequestClose = useCallback(() => {
    setIsExiting(true);
    setIsVisible(false);
  }, []);

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = requestAnimationFrame(() => setIsVisible(true));
    return () => cancelAnimationFrame(frame);
  }, []);

  useEffect(() => {
    if (isVisible || !isExiting) return;
    const reduced = typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!reduced) return;
    finishClose();
  }, [finishClose, isExiting, isVisible]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        handleRequestClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleRequestClose]);

  const handleTransitionEnd = (e: React.TransitionEvent) => {
    if (!isVisible && e.propertyName === 'opacity') {
      finishClose();
    }
  };

  return (
    <>
      <div className="fixed inset-0 z-40" onClick={handleRequestClose} aria-hidden />
      <Hologram
        ref={menuRef}
        variant="corners"
        color={hologramColor}
        role="menu"
        aria-label={ariaLabel}
        onTransitionEnd={handleTransitionEnd}
        className={cn(
          'absolute top-full mt-shell-gap z-50 w-56 p-2 bg-canvas overflow-hidden transition-all motion-reduce:transition-none',
          align === 'right' ? '-right-px origin-top-right' : '-left-px origin-top-left',
          isVisible
            ? 'duration-transition ease-hologram opacity-100 scale-100 [clip-path:inset(0_0_0_0)] motion-reduce:scale-100 motion-reduce:opacity-100'
            : isExiting
              ? 'duration-feedback ease-in opacity-0 scale-y-95 motion-reduce:scale-y-100'
              : cn(
                  'duration-0 opacity-0 scale-95',
                  align === 'right' ? '[clip-path:inset(0_0_100%_100%)]' : '[clip-path:inset(0_100%_100%_0)]',
                ),
          className,
        )}
      >
        <StatusBarMenuContent onClose={handleRequestClose}>
          {children}
        </StatusBarMenuContent>
      </Hologram>
    </>
  );
});

HolographicMenu.displayName = 'HolographicMenu';
