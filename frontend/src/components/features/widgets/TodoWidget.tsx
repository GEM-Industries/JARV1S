import { useState } from 'react';
import {
  CheckCircleIcon,
  CircleIcon,
  ListBulletsIcon,
  EyeIcon,
  EyeSlashIcon,
  TrashIcon,
} from '@phosphor-icons/react';
import { cn } from '../../../utils/cn';
import { WidgetDefinition, BaseWidgetProps, TodoData } from './types';
import { jarvisClient } from '../../../client/JarvisClient';

const TodoHero: React.FC<TodoData & BaseWidgetProps> = ({
  title,
  items = [],
  progress,
}) => {
  const [hideCompleted, setHideCompleted] = useState(false);

  const handleToggle = (taskId: string) => {
    jarvisClient.sendMessage('ui.action', {
      plugin: 'todo',
      tool: 'toggle_task',
      args: { task_id: taskId },
    });
  };

  const handleDelete = (taskId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    jarvisClient.sendMessage('ui.action', {
      plugin: 'todo',
      tool: 'delete_task',
      args: { task_id: taskId },
    });
  };

  const completed = items.filter((i) => i.completed).length;
  const visible = hideCompleted ? items.filter((i) => !i.completed) : items;
  const remaining = items.length - completed;

  return (
    <div className="flex flex-col h-full select-none px-5 py-5 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="flex items-center gap-3 min-w-0">
          <span className="type-heading text-foreground truncate">
            {title}
          </span>
          <div className="w-px h-3 flex flex-col opacity-50 shrink-0">
            <div className="h-[3px] bg-surface-highlight" />
            <div className="flex-1 bg-outline" />
          </div>
          <span className="type-label text-outline shrink-0">
            {remaining} left
          </span>
        </div>
        <button
          onClick={() => setHideCompleted((v) => !v)}
          title={hideCompleted ? 'Show completed' : 'Hide completed'}
          aria-label={hideCompleted ? 'Show completed' : 'Hide completed'}
          className={cn(
            'shrink-0 p-1 rounded transition-colors',
            hideCompleted
              ? 'text-brand'
              : 'text-foreground-subtle hover:text-foreground-muted',
          )}
        >
          {hideCompleted ? (
            <EyeSlashIcon size={16} weight="regular" />
          ) : (
            <EyeIcon size={16} weight="regular" />
          )}
        </button>
      </div>

      {/* Task list — scrollable within available space */}
      <div className="flex-1 flex flex-col gap-1 overflow-y-auto scrollbar-thin scrollbar-thumb-outline/30 pr-1">
        {visible.length === 0 ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-2 text-foreground-disabled">
            <ListBulletsIcon size={32} weight="thin" />
            <span className="type-meta">
              {items.length === 0
                ? 'No tasks'
                : 'Nothing left'}
            </span>
          </div>
        ) : (
          visible.map((item) => (
            <div
              key={item.id}
              className={cn(
                'group/item flex cursor-pointer items-start gap-3 rounded-control px-3 py-2 transition-colors',
                item.completed
                  ? 'bg-surface/20'
                  : 'bg-brand/5 border border-brand/10',
              )}
              onClick={() => handleToggle(item.id)}
            >
              <div className="shrink-0 mt-[3px]">
                {item.completed ? (
                  <CheckCircleIcon
                    size={18}
                    weight="fill"
                    className="text-status-success"
                  />
                ) : (
                  <CircleIcon
                    size={18}
                    weight="light"
                    className="text-outline group-hover/item:text-brand transition-colors"
                  />
                )}
              </div>
              <span
                className={cn(
                  'flex-1 font-body text-sm leading-snug transition-colors min-w-0 break-words',
                  item.completed
                    ? 'text-foreground-muted/50 line-through'
                    : 'text-foreground group-hover/item:text-brand',
                )}
              >
                {item.text}
              </span>
              <button
                onClick={(e) => handleDelete(item.id, e)}
                title="Delete task"
                aria-label="Delete task"
                className="shrink-0 mt-[2px] p-1 rounded opacity-0 group-hover/item:opacity-100 focus:opacity-100 text-foreground-disabled hover:text-status-danger transition-opacity"
              >
                <TrashIcon size={14} weight="regular" />
              </button>
            </div>
          ))
        )}
      </div>

      {/* Footer */}
      <div className="mt-3 pt-3 border-t border-surface-highlight/10 shrink-0">
        <div className="flex items-center justify-between text-outline">
          <span className="text-[10px] font-mono uppercase tracking-widest">
            {title} Status
          </span>
          <span className="text-[10px] font-mono opacity-60">
            {completed}/{items.length} · {Math.round(progress * 100)}% complete
          </span>
        </div>
      </div>
    </div>
  );
};

export const TodoWidget: WidgetDefinition<TodoData> = {
  Hero: TodoHero,
  getCompressedConfig: (data) => ({
    icon: (
      <div className="text-status-success scale-75">
        <CheckCircleIcon
          size={24}
          weight="fill"
          className="drop-shadow-glow-output"
        />
      </div>
    ),
    label: `${Math.round((data.progress || 0) * 100)}%`,
    labelVariant: 'mono',
    width: 'square',
  }),
};
