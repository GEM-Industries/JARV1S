import React, { useEffect, useState } from 'react';
import { ArchiveIcon, EnvelopeIcon, EnvelopeOpenIcon, StarIcon } from '@phosphor-icons/react';
import { WidgetDefinition, BaseWidgetProps } from './types';
import { jarvisClient } from '../../../client/JarvisClient';
import { cn } from '../../../utils/cn';

interface EmailRow {
  id: string;
  subject: string;
  sender: string;
  snippet: string;
  date: string;
  is_unread: boolean;
  labels: string[];
}

interface InboxWidgetData {
  unread_count: number;
  emails: EmailRow[];
}

function _senderName(raw: string): string {
  const match = raw.match(/^"?([^"<]+)"?\s*</);
  if (match) return match[1].trim();
  return raw.split('@')[0] || raw;
}

function _relativeTime(iso: string): string {
  try {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    if (days === 1) return 'yesterday';
    if (days < 7) return `${days}d ago`;
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}

const InboxHero: React.FC<InboxWidgetData & BaseWidgetProps> = ({
  unread_count,
  emails,
}) => {
  const [localEmails, setLocalEmails] = useState(emails);

  useEffect(() => {
    setLocalEmails(emails);
  }, [emails]);

  const visible = localEmails.slice(0, 4);
  const isEmpty = localEmails.length === 0;
  // Reduce the total unread count by however many on-screen emails we've
  // optimistically resolved (marked read or archived) since the last push.
  const resolvedUnread =
    emails.filter((email) => email.is_unread).length -
    localEmails.filter((email) => email.is_unread).length;
  const displayUnreadCount = Math.max(0, unread_count - resolvedUnread);

  const handleArchive = (email: EmailRow, event: React.MouseEvent) => {
    event.stopPropagation();
    const previous = localEmails;
    setLocalEmails((current) => current.filter((item) => item.id !== email.id));
    const sent = jarvisClient.sendMessage('ui.action', {
      plugin: 'gmail',
      tool: 'archive_email',
      args: { message_id: email.id },
    });
    if (!sent.ok) setLocalEmails(previous);
  };

  const handleMarkRead = (email: EmailRow, event: React.MouseEvent) => {
    event.stopPropagation();
    const previous = localEmails;
    setLocalEmails((current) => current.map((item) => (
      item.id === email.id ? { ...item, is_unread: false } : item
    )));
    const sent = jarvisClient.sendMessage('ui.action', {
      plugin: 'gmail',
      tool: 'mark_read',
      args: { message_id: email.id },
    });
    if (!sent.ok) setLocalEmails(previous);
  };

  return (
    <div className="flex flex-col h-full select-none px-5 py-5 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <span className="type-heading text-foreground">
            Inbox
          </span>
          <div className="w-px h-3 flex flex-col opacity-50">
            <div className="h-[3px] bg-surface-highlight" />
            <div className="flex-1 bg-outline" />
          </div>
          {displayUnreadCount > 0 ? (
            <span className="type-label text-brand">
              {displayUnreadCount} unread
            </span>
          ) : (
            <span className="type-label text-foreground-subtle">
              all clear
            </span>
          )}
        </div>
        <EnvelopeIcon
          size={16}
          weight="light"
          className="text-foreground-disabled"
        />
      </div>

      {/* Email rows */}
      <div className="flex-1 flex flex-col gap-1 overflow-hidden">
        {isEmpty ? (
          <div className="flex-1 flex flex-col items-center justify-center gap-2 text-foreground-disabled">
            <EnvelopeOpenIcon size={32} weight="thin" />
            <span className="type-meta">No messages</span>
          </div>
        ) : (
          visible.map((email) => (
            <div
              key={email.id}
              className={cn(
                'group/email flex items-start gap-3 rounded-control px-3 py-2 transition-colors',
                email.is_unread
                  ? 'bg-brand/5 border border-brand/10'
                  : 'bg-surface/30',
              )}
            >
              {/* Unread dot */}
              <div className="mt-1.5 shrink-0">
                {email.is_unread ? (
                  <div className="w-1.5 h-1.5 rounded-full bg-brand" />
                ) : (
                  <div className="w-1.5 h-1.5 rounded-full bg-transparent" />
                )}
              </div>

              {/* Content */}
              <div className="flex-1 min-w-0">
                <div className="flex items-baseline justify-between gap-2">
                  <span
                    className={cn(
                      'type-body truncate',
                      email.is_unread
                        ? 'font-medium text-foreground'
                        : 'font-normal text-foreground-muted',
                    )}
                  >
                    {_senderName(email.sender)}
                  </span>
                  <span className="type-meta tabular-nums text-foreground-subtle shrink-0">
                    {_relativeTime(email.date)}
                  </span>
                </div>
                <p className="type-body text-foreground-disabled truncate mt-0.5">
                  {email.subject || '(No subject)'}
                </p>
              </div>

              <div className="mt-0.5 flex shrink-0 items-center gap-1">
                {email.labels.includes('STARRED') && (
                  <StarIcon
                    size={12}
                    weight="fill"
                    className="text-status-warning"
                  />
                )}
                {email.is_unread && (
                  <button
                    type="button"
                    onClick={(event) => handleMarkRead(email, event)}
                    title="Mark read"
                    aria-label={`Mark ${email.subject || 'email'} as read`}
                    className="rounded-full p-1 text-foreground-disabled opacity-0 transition hover:bg-surface/40 hover:text-brand focus:opacity-100 focus:outline-none focus-visible:ring-1 focus-visible:ring-brand/50 group-hover/email:opacity-100"
                  >
                    <EnvelopeOpenIcon size={13} weight="regular" />
                  </button>
                )}
                <button
                  type="button"
                  onClick={(event) => handleArchive(email, event)}
                  title="Archive"
                  aria-label={`Archive ${email.subject || 'email'}`}
                  className="rounded-full p-1 text-foreground-disabled opacity-0 transition hover:bg-surface/40 hover:text-brand focus:opacity-100 focus:outline-none focus-visible:ring-1 focus-visible:ring-brand/50 group-hover/email:opacity-100"
                >
                  <ArchiveIcon size={13} weight="regular" />
                </button>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Footer */}
      {localEmails.length > 4 && (
        <div className="mt-2 pt-2 border-t border-outline/30">
          <span className="text-[10px] font-mono text-foreground-disabled uppercase tracking-widest">
            +{localEmails.length - 4} more
          </span>
        </div>
      )}
    </div>
  );
};

export const InboxWidget: WidgetDefinition<InboxWidgetData> = {
  Hero: InboxHero,
  getCompressedConfig: (data) => ({
    icon: <EnvelopeIcon size={20} weight="light" />,
    label: data.unread_count > 0 ? `${data.unread_count}` : '✓',
    labelVariant: 'mono',
    width: 'wide',
  }),
};
