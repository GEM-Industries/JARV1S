import React, { useEffect } from 'react';
import { XIcon, PushPinIcon } from '@phosphor-icons/react';
import { UIEnvelope, WidgetSize } from '../../../types';
import { useJarvisStore } from '../../../store/useJarvisStore';
import { getWidgetDefinition, WidgetLoader } from './WidgetRegistry';
import { Hologram } from '../../ui/Hologram';
import { Button } from '../../ui/Button';
import { cn } from '../../../utils/cn';
import { CompressedWidgetView } from './CompressedWidgetView';
import { jarvisClient } from '../../../client/JarvisClient';

interface WidgetWrapperProps {
  envelope: UIEnvelope;
  mode?: 'hero' | 'compressed';
  /** `grid` participates in PrimaryCanvas CSS grid; `stage` is a single focused subject. */
  layoutMode?: 'grid' | 'stage';
  highlighted?: boolean;
  onActivate?: () => void;
}

const sizeMap: Record<WidgetSize, string> = {
  mini: 'col-span-1 md:col-span-3 xl:col-span-3 row-span-1',
  small: 'col-span-1 md:col-span-3 xl:col-span-3 row-span-2',
  wide: 'col-span-1 md:col-span-6 xl:col-span-6 row-span-2',
  tall: 'col-span-1 md:col-span-3 xl:col-span-3 row-span-3',
  large: 'col-span-1 md:col-span-6 xl:col-span-6 row-span-3',
  'large-wide': 'col-span-1 md:col-span-6 xl:col-span-8 row-span-3',
  hero: 'col-span-1 md:col-span-6 xl:col-span-8 row-span-4',
  'full-width': 'col-span-full row-span-3',
};

export const WidgetWrapper: React.FC<WidgetWrapperProps> = ({
  envelope,
  mode = 'hero',
  layoutMode = 'grid',
  highlighted = false,
  onActivate,
}) => {
  const { widget_id, component, data, expires_at, created_at, pinned, layout } = envelope;
  const removeWidget = useJarvisStore((s) => s.removeWidget);
  const toggleWidgetPin = useJarvisStore((s) => s.toggleWidgetPin);
  const connectionState = useJarvisStore((s) => s.connectionState);

  const [timerProgress, setTimerProgress] = React.useState(1);
  const [isExiting, setIsExiting] = React.useState(false);

  const definition = getWidgetDefinition(component);
  const compressedConfig = definition ? definition.getCompressedConfig(data) : null;
  const isWide = compressedConfig?.width === 'wide';
  const isReceipt = compressedConfig?.variant === 'receipt';

  useEffect(() => {
    if (!expires_at || pinned) {
      setTimerProgress(1);
      return;
    }

    const remaining = expires_at - Date.now();
    if (remaining <= 0) {
      removeWidget(widget_id);
      return;
    }

    const timeout = window.setTimeout(() => removeWidget(widget_id), remaining);
    return () => window.clearTimeout(timeout);
  }, [expires_at, widget_id, removeWidget, pinned]);

  useEffect(() => {
    if (mode !== 'hero' || !expires_at || !created_at || pinned) {
      setTimerProgress(1);
      return;
    }

    const updateProgress = () => {
      const remaining = Math.max(0, expires_at - Date.now());
      const total = Math.max(1, expires_at - created_at);
      setTimerProgress(remaining / total);
    };

    updateProgress();
    const interval = window.setInterval(updateProgress, 1000);
    return () => window.clearInterval(interval);
  }, [mode, expires_at, created_at, pinned]);

  if (!definition) {
    console.warn(`Widget definition "${component}" not found in registry.`);
    return null;
  }

  const handleTogglePin = (e?: React.MouseEvent) => {
    e?.stopPropagation();
    const nextPinned = !pinned;
    toggleWidgetPin(widget_id);
    jarvisClient.sendMessage('ui.pin', {
      widget_id,
      pinned: nextPinned,
      ...(nextPinned ? { widget: { ...envelope, pinned: true } } : {}),
    });
  };

  const handleRemove = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (pinned) {
      jarvisClient.sendMessage('ui.pin', { widget_id, pinned: false });
    }
    if (mode === 'hero') {
      setIsExiting(true);
      setTimeout(() => {
        removeWidget(widget_id);
      }, 200);
    } else {
      removeWidget(widget_id);
    }
  };

  const sizeClass = mode === 'hero' && layoutMode === 'grid'
    ? sizeMap[layout?.size || 'small']
    : '';

  const heroSizing = mode === 'hero'
    ? cn(
        "w-full min-w-0 min-h-0",
        layoutMode === 'stage' ? "h-full max-h-full overflow-visible" : "h-full overflow-visible",
        sizeClass,
        "animate-in fade-in zoom-in-[0.98] slide-in-from-top-4 duration-[250ms] ease-snappy-in",
        isExiting && "animate-out fade-out zoom-out-95 slide-out-to-bottom-8 duration-[200ms] ease-snappy-out fill-mode-forwards"
      )
    : cn(
        "cursor-pointer animate-in fade-in slide-in-from-top-4 duration-[250ms] ease-snappy-in",
        isReceipt ? "h-24 w-full" : "h-14",
        !isReceipt && (isWide ? "w-full" : "w-14")
      );

  const hologram = (
    <Hologram 
      variant={mode === 'hero' ? 'base' : 'ringed'} 
      color={
        connectionState === 'error' ? 'error' : 
        connectionState === 'reconnecting' ? 'inactive' : 
        'default'
      }
      style={{ 
        '--hologram-radius': mode === 'compressed' ? (isReceipt ? '18px' : '14px') : '24px',
      } as React.CSSProperties}
      className={cn(
        "flex flex-col pointer-events-auto w-full",
        layoutMode === 'stage' ? "h-full min-h-0" : "h-full",
        mode === 'compressed' && "hover:bg-brand/5 active:scale-95",
        highlighted && "ring-1 ring-brand/60 shadow-[0_0_32px_oklch(var(--color-brand)/0.18)]"
      )}
      onClick={mode === 'compressed' ? onActivate : undefined}
    >
      <div className="p-0 h-full w-full relative overflow-hidden rounded-[var(--hologram-radius)]">
        {mode === 'hero' ? (
          <WidgetLoader
            component={component}
            props={{ ...data, widgetId: widget_id }}
          />
        ) : (
          compressedConfig && (
            <CompressedWidgetView 
              {...compressedConfig}
              layout={isWide ? 'row' : 'stack'}
              subLabel={
                compressedConfig.subLabel
                || (isWide && data && typeof data === 'object' && 'condition' in data ? (data as any).condition : undefined)
              }
            />
          )
        )}

        {expires_at && mode === 'hero' && !pinned && (
          <div className="absolute bottom-0 left-0 right-0 h-[2px] bg-brand/10">
            <div 
              className="h-full bg-brand transition-all duration-100 ease-linear shadow-[0_0_8px_oklch(var(--color-brand))]"
              style={{ width: `${timerProgress * 100}%` }}
            />
          </div>
        )}
      </div>
    </Hologram>
  );

  if (mode !== 'hero') {
    return <div className={cn("group/widget relative", heroSizing)}>{hologram}</div>;
  }

  return (
    <div className={cn("group/widget relative", heroSizing)}>
      {hologram}
      <div className="absolute top-2 right-2 flex items-center gap-1.5 z-30 opacity-0 pointer-events-none group-hover/widget:opacity-100 group-hover/widget:pointer-events-auto transition-opacity duration-150 ease-snappy-in">
        <Button
          onClick={handleTogglePin}
          color="action"
          size="icon"
          className="w-7 h-7 shadow-lg"
          icon={<PushPinIcon size={14} weight={pinned ? "fill" : "bold"} />}
        />
        <Button
          onClick={handleRemove}
          color="danger"
          size="icon"
          className="w-7 h-7 shadow-lg"
          icon={<XIcon size={14} weight="bold" />}
        />
      </div>
    </div>
  );
};
