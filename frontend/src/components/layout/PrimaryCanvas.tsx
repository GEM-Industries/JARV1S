import React, { useEffect, useMemo, useRef } from 'react';
import { useJarvisStore } from '../../store/useJarvisStore';
import { EmptyChatState } from '../features/chat/EmptyChatState';
import {
  LiveStageProjection,
  deriveLiveStagePresentation,
  isPendingApprovalWidget,
  resolveVisibleForegroundWidget,
  useLiveStagePreview,
} from '../features/live-stage';
import { TranscriptWidget } from '../features/transcript/TranscriptWidget';
import { WidgetWrapper } from '../features/widgets/WidgetWrapper';
import { openBackgroundTaskWidget } from '../features/widgets/openBackgroundTaskWidget';
import {
  getReceiptAction,
  isReceiptRailWidget,
  receiptRailPriority,
} from '../features/widgets/widgetRail';
import { cn } from '../../utils/cn';

const MAX_RECEIPT_WIDGETS = 6;

class WidgetErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[WidgetErrorBoundary] render crash:', error, info.componentStack);
  }
  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-32 items-center justify-center opacity-40">
          <span className="type-meta font-mono text-foreground-muted">widget error</span>
        </div>
      );
    }
    return this.props.children;
  }
}

export const PrimaryCanvas: React.FC = () => {
  const hostState = useJarvisStore((s) => s.hostState);
  const connectionState = useJarvisStore((s) => s.connectionState);
  const agentState = useJarvisStore((s) => s.agentState);
  const isSpeaking = useJarvisStore((s) => s.isSpeaking);
  const widgets = useJarvisStore((s) => s.widgets);
  const activeWidgetId = useJarvisStore((s) => s.activeWidgetId);
  const setActiveWidget = useJarvisStore((s) => s.setActiveWidget);
  const transcript = useJarvisStore((s) => s.transcript);
  const partialTranscript = useJarvisStore((s) => s.partialTranscript);
  const liveAssistantPreview = useJarvisStore((s) => s.liveAssistantPreview);
  const isTranscriptVisible = useJarvisStore((s) => s.isTranscriptVisible);

  const isConnected = connectionState === 'connected';
  const hasContent = transcript.length > 0 || !!partialTranscript;
  const allWidgets = useMemo(() => Object.values(widgets), [widgets]);

  const presentation = useMemo(
    () => deriveLiveStagePresentation({
      hostState,
      connectionState,
      agentState,
      isSpeaking,
      transcript,
      partialTranscript,
      liveAssistantPreview,
      activeWidgetId,
      widgets: allWidgets,
    }),
    [
      hostState,
      connectionState,
      agentState,
      isSpeaking,
      transcript,
      partialTranscript,
      liveAssistantPreview,
      activeWidgetId,
      allWidgets,
    ],
  );

  const visibleForeground = resolveVisibleForegroundWidget(presentation);
  const focalRef = useRef<HTMLDivElement>(null);
  const lastFocusedConsentId = useRef<string | null>(null);
  const showStagePreview = useLiveStagePreview(presentation, isTranscriptVisible);

  useEffect(() => {
    if (
      presentation.focalKind !== 'widget'
      || !visibleForeground
      || !isPendingApprovalWidget(visibleForeground)
    ) {
      lastFocusedConsentId.current = null;
      return;
    }
    if (lastFocusedConsentId.current === visibleForeground.widget_id) return;
    lastFocusedConsentId.current = visibleForeground.widget_id;
    focalRef.current?.focus({ preventScroll: true });
  }, [presentation.focalKind, visibleForeground]);

  const receiptWidgets = useMemo(
    () => allWidgets
      .filter(isReceiptRailWidget)
      .sort((a, b) => (
        receiptRailPriority(b) - receiptRailPriority(a)
        || (b.created_at || 0) - (a.created_at || 0)
      ))
      .slice(0, MAX_RECEIPT_WIDGETS),
    [allWidgets],
  );

  const handleReceiptActivate = (envelope: (typeof receiptWidgets)[number]) => {
    const action = getReceiptAction(envelope);

    if (action?.type === 'activate_widget' && action.widget_id) {
      if (widgets[action.widget_id]) {
        setActiveWidget(action.widget_id);
      } else if (action.task_id) {
        void openBackgroundTaskWidget(action.task_id);
      }
      return;
    }

    if (action?.type === 'open_background_task' && action.task_id) {
      void openBackgroundTaskWidget(action.task_id);
    } else {
      setActiveWidget(envelope.widget_id);
    }
  };

  return (
    <div className="relative flex h-full w-full overflow-hidden">
      <div className={cn(
        'relative h-full shrink-0 pt-safe-top pb-safe-bottom',
        isConnected && hasContent && isTranscriptVisible && 'pr-3',
      )}>
        {isConnected && hasContent && <TranscriptWidget />}
      </div>

      <div className="relative min-h-0 min-w-0 flex-1 pt-safe-top pb-safe-bottom">
        <div className="flex h-full min-h-0 min-w-0 flex-col px-6 pt-6 pb-6">
          <div className="relative flex min-h-0 flex-1 flex-col items-center justify-center">
            <div className="flex min-h-64 w-full max-w-5xl flex-1 flex-col items-center justify-center">
              {presentation.focalKind === 'recovery' && (
                <LiveStageProjection presentation={presentation} />
              )}

              {presentation.focalKind === 'projection' && (
                <LiveStageProjection
                  presentation={presentation}
                  showPreview={showStagePreview}
                />
              )}

              {presentation.focalKind === 'onboarding' && isConnected && (
                <EmptyChatState />
              )}

              {presentation.focalKind === 'widget' && visibleForeground && (
                <div
                  ref={focalRef}
                  tabIndex={isPendingApprovalWidget(visibleForeground) ? -1 : undefined}
                  className="flex w-full max-w-4xl flex-1 items-center justify-center py-2 outline-none focus-visible:ring-2 focus-visible:ring-brand/70 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
                >
                  <div className="max-h-full min-h-[16rem] w-full">
                    <WidgetErrorBoundary key={visibleForeground.widget_id}>
                      <WidgetWrapper
                        envelope={visibleForeground}
                        mode="hero"
                        layoutMode="stage"
                        highlighted
                      />
                    </WidgetErrorBoundary>
                  </div>
                </div>
              )}

              {presentation.focalKind === 'empty' && (
                <div className="h-24 w-full" aria-hidden />
              )}
            </div>
          </div>

          {presentation.pinnedSupport.length > 0 && (
            <div className="mt-6 flex w-full max-w-5xl flex-wrap justify-center gap-3 self-center">
              {presentation.pinnedSupport.map((envelope) => (
                <WidgetErrorBoundary key={envelope.widget_id}>
                  <WidgetWrapper
                    envelope={envelope}
                    mode="compressed"
                    highlighted={envelope.widget_id === activeWidgetId}
                    onActivate={() => setActiveWidget(envelope.widget_id)}
                  />
                </WidgetErrorBoundary>
              ))}
            </div>
          )}
        </div>

        {receiptWidgets.length > 0 && (
          <aside
            aria-label="Receipts"
            className="pointer-events-none fixed right-6 top-shell-overlay bottom-safe-bottom z-20 flex w-72 flex-col gap-3 overflow-y-auto"
          >
            <div className="flex flex-col gap-3 pointer-events-auto">
              {receiptWidgets.map((envelope) => (
                <WidgetErrorBoundary key={envelope.widget_id}>
                  <WidgetWrapper
                    envelope={envelope}
                    mode="compressed"
                    highlighted={
                      envelope.widget_id === activeWidgetId
                      || getReceiptAction(envelope)?.widget_id === activeWidgetId
                      || presentation.attentionReceiptIds.includes(envelope.widget_id)
                    }
                    onActivate={() => handleReceiptActivate(envelope)}
                  />
                </WidgetErrorBoundary>
              ))}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
};
