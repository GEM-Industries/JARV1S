import { useJarvisStore } from '../../../store/useJarvisStore';
import type { UIEnvelope } from '../../../types';
import { authorizedFetch } from '../../../client/http';

export async function openBackgroundTaskWidget(
  taskId: string,
  options?: { pinned?: boolean },
): Promise<boolean> {
  const resp = await authorizedFetch(`/api/v1/tasks/${taskId}`);
  if (!resp.ok) return false;

  const doc = await resp.json() as Record<string, unknown>;
  const widgetId = `task-${taskId}`;
  const envelope: UIEnvelope = {
    widget_id: widgetId,
    component: 'BackgroundTaskWidget',
    data: {
      task_id: taskId,
      status: doc.status ?? 'running',
      progress_summary: doc.progress_summary ?? '',
      live_status: doc.live_status,
      attention: doc.attention ?? 'none',
      pending_input: doc.pending_input,
      source: doc.source ?? 'voice',
      mode: doc.mode,
      created_at: typeof doc.created_at === 'string'
        ? doc.created_at
        : new Date().toISOString(),
      artifacts: doc.artifacts ?? [],
      activity: doc.activity ?? [],
    },
    layout: { size: 'wide', priority: 50 },
    title: 'Background Task',
    pinned: options?.pinned ?? false,
  };

  const store = useJarvisStore.getState();
  store.upsertWidget(envelope);
  store.setActiveWidget(widgetId);
  return true;
}
