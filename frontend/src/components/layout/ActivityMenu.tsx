import React, { useEffect, useState } from 'react';
import { ArrowRightIcon } from '@phosphor-icons/react';
import { operationsApi } from '../../client/operationsApi';
import { useJarvisStore } from '../../store/useJarvisStore';
import { ActivityRow } from '../features/operations/ActivityTimeline';
import type { ActivityItem } from '../../types/operations';
import { Button } from '../ui/Button';
import { Divider } from '../ui/Divider';
import { MenuSectionHeader } from '../ui/holographic-menu';
import { Placeholder } from '../ui/Placeholder';

const RECENT_ACTIVITY_LIMIT = 7;

export const RecentActivityPopover: React.FC<{
  onClose: () => void;
}> = ({ onClose }) => {
  const runsVersion = useJarvisStore((s) => s.runsVersion);
  const [items, setItems] = useState<ActivityItem[]>([]);
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading');

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      // Keep current rows while refreshing so the host does not open tall then shrink.
      setState((prev) => (prev === 'ready' ? 'ready' : 'loading'));
      try {
        const data = await operationsApi.activity('all', RECENT_ACTIVITY_LIMIT);
        if (!cancelled) {
          setItems(data);
          setState('ready');
        }
      } catch {
        if (!cancelled) setState('error');
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [runsVersion]);

  return (
    <>
      <MenuSectionHeader>Recent activity</MenuSectionHeader>

      <div className="max-h-[min(70vh,520px)] overflow-y-auto px-2 py-2 space-y-2">
        {state === 'loading' && <Placeholder>Loading activity…</Placeholder>}
        {state === 'error' && <Placeholder tone="error">Could not load activity.</Placeholder>}
        {state === 'ready' && items.length === 0 && (
          <Placeholder>No recent reminders or runs.</Placeholder>
        )}
        {state === 'ready' &&
          items.map((item) => (
            <ActivityRow key={`${item.kind}:${item.id}`} item={item} onClose={onClose} />
          ))}
      </div>
      <Divider variant="simple" className="mx-2 opacity-60" />
      <div className="px-2 py-2">
        <Button
          variant="ghost"
          color="action"
          size="sm"
          onClick={() => useJarvisStore.getState().openOverlay('operations')}
          className="w-full opacity-80 hover:opacity-100"
        >
          <span className="flex items-center justify-center gap-2 px-1">
            View all activity
            <ArrowRightIcon size={12} weight="bold" />
          </span>
        </Button>
      </div>
    </>
  );
};
