import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeftIcon,
  CheckCircleIcon,
  CpuIcon,
  LinkIcon,
  LockKeyIcon,
  MagnifyingGlassIcon,
  PlugIcon,
  ShieldCheckIcon,
  SpinnerIcon,
  WarningIcon,
} from '@phosphor-icons/react';
import { integrationsApi, type CatalogItem } from '../../../client/integrationsApi';
import { OAuthApiError, oauthApi, type ProviderStatus } from '../../../client/oauthApi';
import { useJarvisStore } from '../../../store/useJarvisStore';
import type { IntegrationSummary } from '../../../types';
import { cn } from '../../../utils/cn';
import {
  beginOAuthAuthorization,
  closeOAuthPopup,
  watchOAuthCompletion,
} from '../../../utils/oauthFlow';
import { Button } from '../../ui/Button';
import { Disclosure } from '../../ui/Disclosure';
import { StatusPill, type StatusTone } from '../../ui/StatusPill';
import { SearchField } from '../../ui/FieldControl';
import { SegmentedTabs } from '../../ui/SegmentedTabs';
import { DataField } from '../../ui/PanelSection';
import { Switch } from '../../ui/Switch';
import { EmptyState } from '../../ui/EmptyState';
import { SectionHeader } from '../../ui/SectionHeader';
import { StatusBarWorkspaceHeader } from '../../ui/StatusBarWorkspaceHeader';
import { TextLink } from '../../ui/TextLink';
import { ConnectionList } from './ConnectionList';
import {
  COMPOSIO_CONNECTOR_LABEL,
  connectionIds,
  connectionLabel,
  connectionSummary,
  oauthRedirectUri,
} from './connections';

type RowPhase =
  | 'idle'
  | 'connecting'
  | 'disconnecting'
  | 'reconciling'
  | 'confirm_disconnect';

interface RowState {
  phase: RowPhase;
  error?: string;
  provider?: string;
}

type CapabilityGroup = 'Find & read' | 'Create & update' | 'Other actions';

function errMsg(e: unknown, fb: string) {
  return e instanceof Error ? e.message : fb;
}

function metaText(item: IntegrationSummary) {
  if (item.connected && item.loaded) return `${item.tool_count} tool${item.tool_count !== 1 ? 's' : ''} active`;
  if (item.connected && !item.loaded) return 'Connected — loading tools…';
  if (item.status === 'error') return 'Unavailable';
  return 'Ready to connect';
}

/** Labeled attention state for list rows — null when healthy (subtitle is enough). */
function attentionPill(item: IntegrationSummary): { tone: StatusTone; label: string } | null {
  if (item.status === 'error' || item.health === 'unavailable') {
    return { tone: 'error', label: 'Unavailable' };
  }
  if (item.health === 'degraded') {
    return { tone: 'warning', label: 'Setup needed' };
  }
  if (item.kind === 'built_in' && !item.enabled) {
    return { tone: 'off', label: 'Disabled' };
  }
  if (item.connected && !item.loaded) {
    return { tone: 'warning', label: 'Loading' };
  }
  return null;
}

function titleCase(value: string) {
  return value.replace(/[_-]/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

function matchesQuery(haystack: string, query: string) {
  if (!query) return true;
  return haystack.toLowerCase().includes(query);
}

function normalizeCapabilityLabel(raw: string, appName?: string, displayName?: string) {
  let label = raw.trim();
  const prefixes = [
    displayName,
    appName,
    displayName?.replace(/\s+/g, '_'),
    appName?.replace(/\s+/g, '_'),
  ]
    .filter(Boolean)
    .map((value) => String(value).toLowerCase());

  for (const prefix of prefixes) {
    const escaped = prefix.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    label = label.replace(new RegExp(`^${escaped}[\\s_:-]+`, 'i'), '');
  }

  return titleCase(label);
}

function capabilityGroup(label: string): CapabilityGroup {
  const lower = label.toLowerCase();
  if (
    /\b(find|search|list|get|fetch|retrieve|read|lookup|status|history|details?|info)\b/.test(lower)
  ) {
    return 'Find & read';
  }
  if (
    /\b(create|add|send|update|upload|write|post|set|schedule|dispatch|resume|cancel|delete|remove|edit)\b/.test(lower)
  ) {
    return 'Create & update';
  }
  return 'Other actions';
}

function groupCapabilities(
  capabilities: string[],
  appName?: string,
  displayName?: string,
): Array<{ group: CapabilityGroup; items: string[] }> {
  const buckets: Record<CapabilityGroup, string[]> = {
    'Find & read': [],
    'Create & update': [],
    'Other actions': [],
  };

  const seen = new Set<string>();
  for (const capability of capabilities) {
    const label = normalizeCapabilityLabel(capability, appName, displayName);
    const key = label.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    buckets[capabilityGroup(label)].push(label);
  }

  return (Object.keys(buckets) as CapabilityGroup[])
    .map((group) => ({ group, items: buckets[group] }))
    .filter((entry) => entry.items.length > 0);
}

const formatLastUsed = (value?: string | null) => {
  if (!value) return 'No recent activity';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'No recent activity' : date.toLocaleString();
};

/* ──────────────────────── Skeleton Row ──────────────────────── */

const SkeletonRow: React.FC<{ delay?: number }> = ({ delay = 0 }) => (
  <div
    className="flex items-center gap-3 px-4 py-3 animate-pulse"
    style={{ animationDelay: `${delay}ms` }}
  >
    <div className="h-2 w-2 rounded-full bg-outline/30" />
    <div className="flex-1 space-y-2">
      <div className="h-3 w-28 rounded bg-outline/20" />
      <div className="h-2.5 w-20 rounded bg-outline/10" />
    </div>
  </div>
);

/* ─────────────────────── App List Row ───────────────────── */

interface AppListRowProps {
  item: IntegrationSummary;
  selected: boolean;
  busy?: boolean;
  error?: string;
  subtitle: string;
  onSelect: (name: string) => void;
}

const AppListRow: React.FC<AppListRowProps> = ({
  item,
  selected,
  busy = false,
  error,
  subtitle,
  onSelect,
}) => {
  const attention = attentionPill(item);

  return (
    <button
      type="button"
      onClick={() => onSelect(item.name)}
      aria-current={selected ? 'true' : undefined}
      className={cn(
        'group mx-4 flex min-h-14 w-[calc(100%-2rem)] items-center gap-3 rounded-control px-3 py-3 text-left transition-colors',
        'ui-surface-selectable focus:outline-none',
        selected ? 'ui-surface-selected' : null,
        busy && 'pointer-events-none opacity-40',
      )}
    >
      <div className="min-w-0 flex-1">
        <p className="truncate type-label text-foreground">
          {item.display_name}
        </p>
        <p className="mt-1 truncate type-meta text-foreground-subtle">
          {subtitle}
        </p>
        {error && (
          <p className="mt-1 type-meta text-status-danger/90">{error}</p>
        )}
      </div>
      {attention ? (
        <StatusPill tone={attention.tone} className="shrink-0">
          {attention.label}
        </StatusPill>
      ) : null}
    </button>
  );
};

/* ─────────────────────── Catalog Card ─────────────────────── */

interface CatalogCardProps {
  item: CatalogItem;
  phase: RowPhase;
  error?: string;
  onConnect: (slug: string) => void;
}

const CatalogCard: React.FC<CatalogCardProps> = ({ item, phase, error, onConnect }) => {
  const busy = phase === 'connecting';

  return (
    <article
      className={cn(
        'flex min-h-40 flex-col gap-3 rounded-panel border border-outline/15 bg-surface/10 p-4',
        busy && 'opacity-50',
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate type-heading text-foreground">{item.display_name}</h3>
          <p className="mt-1 type-meta text-foreground-subtle">
            {item.managed_auth ? COMPOSIO_CONNECTOR_LABEL : 'Custom setup required'}
          </p>
        </div>
        {item.connected ? (
          <span className="shrink-0 type-meta text-status-success">Connected</span>
        ) : null}
      </div>

      <p className="line-clamp-3 flex-1 type-body text-foreground-muted">
        {item.description || 'Connect this service so JARV1S can use its tools.'}
      </p>

      {error && (
        <p className="type-meta text-status-danger/90">{error}</p>
      )}

      <div className="mt-auto flex min-h-10 items-center justify-between gap-3">
        {!item.connected && !item.managed_auth ? (
          <TextLink
            href="https://platform.composio.dev"
            external
            className="type-label-small"
          >
            Setup guide
          </TextLink>
        ) : (
          <span />
        )}

        {busy ? (
          <SpinnerIcon size={16} className="animate-spin text-brand" />
        ) : item.connected ? null : (
          <Button
            size="sm"
            variant="ghost"
            color="brand"
            shape="control"
            disabled={busy}
            onClick={() => onConnect(item.slug)}
          >
            Connect
          </Button>
        )}
      </div>
    </article>
  );
};

/* ─────────────────── Capability Explorer ─────────────────── */

const CapabilityExplorer: React.FC<{
  capabilities: string[];
  appName: string;
  displayName: string;
}> = ({ capabilities, appName, displayName }) => {
  const groups = useMemo(
    () => groupCapabilities(capabilities, appName, displayName),
    [capabilities, appName, displayName],
  );
  const total = groups.reduce((sum, group) => sum + group.items.length, 0);

  if (!total) {
    return (
      <p className="type-meta text-foreground-subtle">
        Actions appear after this app is connected and tools are loaded.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {groups.map(({ group, items }) => {
        return (
          <div key={group}>
            <p className="mb-2 type-label-small text-foreground-subtle">
              {group}
              <span className="ml-2 tabular-nums text-foreground-subtle">{items.length}</span>
            </p>
            <ul className="ui-surface-group">
              {items.map((item) => (
                <li
                  key={item}
                  className="bg-canvas-sunken/25 px-3 py-2.5 type-body text-foreground"
                >
                  {item}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
};

/* ─────────────────── Section Label ────────────────────────── */

const SectionLabel: React.FC<{
  icon: React.ReactNode;
  label: string;
  count?: number;
}> = ({ icon, label, count }) => (
  <SectionHeader icon={icon} label={label} count={count} className="px-4" />
);

const ProviderSetupPanel: React.FC<{
  provider: string;
  busy?: boolean;
  error?: string;
  onSave: (clientId: string, clientSecret?: string) => void;
  onCancel: () => void;
}> = ({ provider, busy = false, error, onSave, onCancel }) => {
  const redirectUri = oauthRedirectUri(provider);
  const requiresSecret = provider === 'google';
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
  const [copied, setCopied] = useState(false);

  const copyRedirectUri = async () => {
    await navigator.clipboard.writeText(redirectUri);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  return (
    <div className="rounded-control border border-brand/20 bg-brand/[0.04] px-4 py-3">
      <div className="flex items-start gap-3">
        <LockKeyIcon size={15} weight="fill" className="mt-1 text-brand/80" />
        <div className="min-w-0 flex-1">
          <p className="type-label-small text-foreground-subtle">
            OAuth setup
          </p>
          <p className="mt-1 type-heading text-foreground">
            Set up {connectionLabel(provider)}
          </p>
          <p className="mt-1 type-body text-foreground-muted">
            Add credentials once, then continue with the normal consent flow.
          </p>

          <div className="mt-3 space-y-2">
            <label className="block type-label-small text-foreground-subtle">
              Redirect URI
            </label>
            <button
              type="button"
              onClick={() => void copyRedirectUri()}
              className="flex h-11 w-full items-center justify-between gap-3 rounded-control border border-outline/15 bg-canvas-sunken/35 px-3 text-left transition-colors hover:border-brand/35 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/60"
            >
              <span className="truncate font-mono text-meta text-foreground-muted">{redirectUri}</span>
              <span className="shrink-0 type-label-small text-brand/70">
                {copied ? 'Copied' : 'Copy'}
              </span>
            </button>
          </div>

          <div className="mt-3 grid gap-2">
            <input
              type="text"
              value={clientId}
              onChange={(e) => setClientId(e.target.value)}
              placeholder={provider === 'microsoft' ? 'Application (client) ID' : 'Client ID'}
              className={cn(
                'h-11 w-full rounded-control border border-outline/20 bg-canvas-sunken/35 px-3',
                'text-sm font-mono text-foreground placeholder:text-foreground-disabled/35',
                'outline-none transition-colors focus:border-brand/35 focus-visible:ring-2 focus-visible:ring-brand/50',
              )}
            />
            {requiresSecret && (
              <input
                type="password"
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                placeholder="Client Secret"
                className={cn(
                  'h-11 w-full rounded-control border border-outline/20 bg-canvas-sunken/35 px-3',
                  'text-sm font-mono text-foreground placeholder:text-foreground-disabled/35',
                  'outline-none transition-colors focus:border-brand/35 focus-visible:ring-2 focus-visible:ring-brand/50',
                )}
              />
            )}
          </div>

          {error && (
            <p className="mt-2 flex items-start gap-2 text-xs font-body leading-snug text-status-danger">
              <WarningIcon size={12} className="mt-0.5 shrink-0" />
              <span>{error}</span>
            </p>
          )}

          <div className="mt-3 flex items-center gap-2">
            <Button
              variant="ghost"
              color="brand"
              size="sm"
              shape="control"
              disabled={busy || !clientId.trim()}
              onClick={() => onSave(clientId.trim(), requiresSecret ? clientSecret.trim() : undefined)}
            >
              {busy ? 'Saving…' : 'Save and connect'}
            </Button>
            <Button color="neutral" size="sm" variant="ghost" shape="control" disabled={busy} onClick={onCancel}>
              Cancel
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
};

interface AppDetailProps {
  item: IntegrationSummary;
  state: RowState;
  toggling: boolean;
  providerStatuses: Record<string, ProviderStatus>;
  onToggle: (name: string, enabled: boolean) => void;
  onConnect: (name: string) => void;
  onRecover: (name: string) => void;
  onAuthorizeConnection: (name: string, connectionId: string) => void;
  onSetupConnection: (name: string, connectionId: string) => void;
  onConfirmDisconnect: (name: string, provider?: string) => void;
  onDisconnect: (name: string, provider?: string) => void;
  onCancelDisconnect: (name: string) => void;
  children?: React.ReactNode;
}

const AppDetail: React.FC<AppDetailProps> = ({
  item,
  state,
  toggling,
  providerStatuses,
  onToggle,
  onConnect,
  onRecover,
  onAuthorizeConnection,
  onSetupConnection,
  onConfirmDisconnect,
  onDisconnect,
  onCancelDisconnect,
  children,
}) => {
  const busy = toggling || ['connecting', 'disconnecting', 'reconciling'].includes(state.phase);
  const sources = connectionIds(item);
  const attention = attentionPill(item);
  const capabilityCount = item.capabilities.length;
  const hasActionRow = state.phase === 'confirm_disconnect'
    || item.auth_type === 'composio'
    || item.kind === 'composio'
    || (item.connected && !item.loaded);

  const facts: Array<{ label: string; value: string }> = [];
  if (item.connection !== 'connected' || item.health !== 'healthy') {
    facts.push({ label: 'Status', value: titleCase(item.connection) });
  }
  if (item.health !== 'healthy') {
    facts.push({ label: 'Health', value: titleCase(item.health) });
  }
  if (item.account) {
    facts.push({ label: 'Account', value: item.account });
  }
  if (item.last_used_at) {
    facts.push({ label: 'Last used', value: formatLastUsed(item.last_used_at) });
  }
  if (item.kind === 'composio' || item.auth_type === 'composio') {
    facts.push({ label: 'Connector', value: COMPOSIO_CONNECTOR_LABEL });
  }

  return (
    <div className="space-y-5 p-6">
      <div>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <h3 className="type-title text-foreground">{item.display_name}</h3>
              {attention ? (
                <StatusPill tone={attention.tone}>{attention.label}</StatusPill>
              ) : null}
            </div>
            <p className="mt-2 type-body text-foreground-muted">
              {item.description || `${item.tool_count} capabilities available to JARV1S.`}
            </p>
          </div>
          {item.kind === 'built_in' && (
            <Switch
              checked={item.enabled}
              disabled={busy}
              onChange={(checked) => onToggle(item.name, checked)}
              label={item.enabled ? 'Enabled' : 'Disabled'}
              className="w-auto shrink-0 gap-3"
            />
          )}
        </div>
        {(state.error || item.last_error) && (
          <p className="mt-3 type-body text-status-danger">{state.error || item.last_error}</p>
        )}

        {hasActionRow && (
          <div className="mt-4 flex flex-wrap items-center gap-2">
            {state.phase === 'confirm_disconnect' ? (
              <>
                <span className="type-body text-foreground-muted">Disconnect this connection?</span>
                <Button size="md" color="critical" shape="control" onClick={() => onDisconnect(item.name, state.provider)}>
                  Confirm disconnect
                </Button>
                <Button size="md" variant="ghost" color="neutral" shape="control" onClick={() => onCancelDisconnect(item.name)}>
                  Cancel
                </Button>
              </>
            ) : item.auth_type === 'composio' ? (
              item.connected ? (
                <Button
                  size="md"
                  variant="ghost"
                  color="danger"
                  shape="control"
                  disabled={busy}
                  onClick={() => onConfirmDisconnect(item.name)}
                >
                  Disconnect
                </Button>
              ) : (
                <Button size="md" variant="ghost" color="brand" shape="control" disabled={busy} onClick={() => onConnect(item.name)}>
                  Reconnect
                </Button>
              )
            ) : item.connected && !item.loaded ? (
              <>
                <Button size="md" variant="ghost" color="brand" shape="control" disabled={busy} onClick={() => onRecover(item.name)}>
                  Reload tools
                </Button>
                <Button
                  size="md"
                  variant="ghost"
                  color="danger"
                  shape="control"
                  disabled={busy}
                  onClick={() => onConfirmDisconnect(item.name)}
                >
                  Disconnect
                </Button>
              </>
            ) : item.connected && item.kind === 'composio' ? (
              <Button
                size="md"
                variant="ghost"
                color="danger"
                shape="control"
                disabled={busy}
                onClick={() => onConfirmDisconnect(item.name)}
              >
                Disconnect
              </Button>
            ) : !item.connected && item.kind === 'composio' ? (
              <Button size="md" variant="ghost" color="brand" shape="control" disabled={busy} onClick={() => onConnect(item.name)}>
                Connect
              </Button>
            ) : (
              null
            )}
          </div>
        )}
      </div>

      {facts.length > 0 && (
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 border-t border-outline/15 pt-4">
          {facts.map((fact) => (
            <DataField key={fact.label} label={fact.label} value={fact.value} />
          ))}
        </dl>
      )}

      {state.phase !== 'confirm_disconnect' && sources.length > 0 ? (
        <ConnectionList
          item={item}
          providerStatuses={providerStatuses}
          busyId={state.phase === 'connecting' ? state.provider : undefined}
          onConnect={onAuthorizeConnection}
          onSetup={onSetupConnection}
          onDisconnect={onConfirmDisconnect}
        />
      ) : null}

      {state.phase !== 'confirm_disconnect' ? children : null}

      {capabilityCount > 0 ? (
        <Disclosure
          label={`${capabilityCount} action${capabilityCount === 1 ? '' : 's'} JARV1S can perform`}
          className="border-t border-outline/15 pt-1"
          summaryClassName="type-label text-foreground-muted"
          contentClassName="pb-1 pt-3"
        >
          <CapabilityExplorer
            capabilities={item.capabilities}
            appName={item.name}
            displayName={item.display_name}
          />
        </Disclosure>
      ) : null}
    </div>
  );
};

/* ────────────────────── Main Panel ───────────────────────── */

type PanelTab = 'my_apps' | 'discover';

export const IntegrationsPanelContent: React.FC = () => {
  const closeOverlay = useJarvisStore((s) => s.closeOverlay);

  const [activeTab, setActiveTab] = useState<PanelTab>('my_apps');
  const [selectedName, setSelectedName] = useState<string | null>(null);

  const [integrations, setIntegrations] = useState<IntegrationSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [rowStates, setRowStates] = useState<Record<string, RowState>>({});
  const [toggleStates, setToggleStates] = useState<Record<string, { toggling: boolean; error?: string }>>({});
  const [providerStatuses, setProviderStatuses] = useState<Record<string, ProviderStatus>>({});

  const [listQuery, setListQuery] = useState('');
  const [catalogQuery, setCatalogQuery] = useState('');
  const [catalogItems, setCatalogItems] = useState<CatalogItem[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [catalogRowStates, setCatalogRowStates] = useState<Record<string, RowState>>({});
  const catalogDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [setupTarget, setSetupTarget] = useState<{ name: string; provider: string } | null>(null);
  const [setupError, setSetupError] = useState<string | undefined>();

  const popupRef = useRef<Window | null>(null);
  const popupCleanupRef = useRef<(() => void) | null>(null);
  const activeConnectTarget = useRef<{ source: 'list' | 'catalog'; name: string } | null>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);

  const clearPopup = useCallback(() => {
    popupCleanupRef.current?.();
    popupCleanupRef.current = null;
    popupRef.current = null;
  }, []);

  const fetchList = useCallback(async ({ silent = false }: { silent?: boolean } = {}) => {
    if (!silent) { setLoading(true); setFetchError(null); }
    try {
      const data = await integrationsApi.list();
      setIntegrations(data.items);
      setSelectedName((current) => (
        current && data.items.some((item) => item.name === current)
          ? current
          : data.items.find((item) => item.kind === 'composio' && item.connected)?.name
            ?? data.items.find((item) => item.kind === 'built_in')?.name
            ?? null
      ));
      return data.items;
    } catch (e) {
      const msg = errMsg(e, 'Could not load apps.');
      if (!silent) setFetchError(msg);
      throw new Error(msg);
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  const loadProviderStatuses = useCallback(async () => {
    try {
      const providers = await oauthApi.getProviders();
      setProviderStatuses(Object.fromEntries(providers.map((provider) => [provider.provider, provider])));
    } catch {
      setProviderStatuses({});
    }
  }, []);

  const setRow = useCallback((name: string, phase: RowPhase, error?: string, provider?: string) => {
    setRowStates((prev) => ({ ...prev, [name]: { phase, error, provider } }));
  }, []);

  const getRow = useCallback(
    (name: string): RowState => rowStates[name] ?? { phase: 'idle' },
    [rowStates],
  );

  const setCatalogRow = useCallback((slug: string, phase: RowPhase, error?: string) => {
    setCatalogRowStates((prev) => ({ ...prev, [slug]: { phase, error } }));
  }, []);

  const syncRow = useCallback(async (name: string, fallbackErr?: string) => {
    try {
      const items = await fetchList({ silent: true });
      const item = items.find((i) => i.name === name);
      const err = item?.status === 'error' ? (item.last_error ?? fallbackErr) : fallbackErr;
      setRow(name, 'idle', err);
    } catch (e) {
      setRow(name, 'idle', errMsg(e, fallbackErr ?? 'Could not refresh status.'));
    }
  }, [fetchList, setRow]);

  const searchCatalog = useCallback(async (query: string) => {
    setCatalogLoading(true);
    setCatalogError(null);
    try {
      const data = await integrationsApi.searchCatalog(query);
      setCatalogItems(data.items);
      setCatalogError(null);
    } catch (e) {
      setCatalogError(errMsg(e, 'Could not search catalog.'));
      setCatalogItems([]);
    } finally {
      // Always mark loaded so a 503 (e.g. Composio unconfigured) does not
      // re-trigger the discover auto-fetch loop via catalogLoading flips.
      setCatalogLoaded(true);
      setCatalogLoading(false);
    }
  }, []);

  const handleCatalogQueryChange = useCallback((value: string) => {
    setCatalogQuery(value);
    if (catalogDebounceRef.current) clearTimeout(catalogDebounceRef.current);
    catalogDebounceRef.current = setTimeout(() => {
      void searchCatalog(value.trim());
    }, 350);
  }, [searchCatalog]);

  useEffect(() => {
    void fetchList();
    void loadProviderStatuses();
  }, [fetchList, loadProviderStatuses]);

  useEffect(() => {
    if (activeTab !== 'discover' || catalogLoaded || catalogLoading) return;
    void searchCatalog('');
  }, [activeTab, catalogLoaded, catalogLoading, searchCatalog]);

  useEffect(() => () => clearPopup(), [clearPopup]);

  const connectViaPopup = useCallback(async (name: string, label: string, source: 'list' | 'catalog') => {
    const setErr = source === 'catalog' ? setCatalogRow : setRow;
    setErr(name, 'connecting');
    try {
      const { connect_url } = await integrationsApi.connectLink(name);
      const launch = await beginOAuthAuthorization(label, connect_url);
      closeOAuthPopup(popupRef.current);
      popupRef.current = launch.popup ?? null;
      activeConnectTarget.current = { source, name };
      popupCleanupRef.current = watchOAuthCompletion({
        app: name,
        mode: launch.mode,
        popup: launch.popup,
        checkComplete: () => integrationsApi.reconcile(name).then((r) => r.success),
        onComplete: (msg) => {
          clearPopup();
          const target = activeConnectTarget.current;
          activeConnectTarget.current = null;
          if (target?.source === 'catalog') {
            setCatalogRow(msg.app, 'idle', msg.success ? undefined : 'Authorization failed.');
            void fetchList({ silent: true });
            void searchCatalog(catalogQuery.trim());
          } else {
            void syncRow(msg.app, msg.success ? undefined : 'Authorization failed.');
          }
        },
        onAborted: () => {
          clearPopup();
          const target = activeConnectTarget.current;
          activeConnectTarget.current = null;
          const message = launch.mode === 'external'
            ? 'Authorization timed out. Finish sign-in in your browser, then try again.'
            : 'Auth window closed before finishing.';
          if (target?.source === 'catalog') {
            setCatalogRow(name, 'idle', message);
          } else {
            void syncRow(name, message);
          }
        },
      });
    } catch (e) {
      clearPopup();
      setErr(name, 'idle', errMsg(e, 'Could not generate an authorization link.'));
    }
  }, [setCatalogRow, setRow, clearPopup, syncRow, fetchList, searchCatalog, catalogQuery]);

  const handleConnect = useCallback((name: string) => connectViaPopup(name, `Connect ${name}`, 'list'), [connectViaPopup]);

  const handleCatalogConnect = useCallback((slug: string) => connectViaPopup(slug, `Connect ${slug}`, 'catalog'), [connectViaPopup]);

  const handleRecover = async (name: string) => {
    setRow(name, 'reconciling');
    try {
      const res = await integrationsApi.reconcile(name);
      await syncRow(name, res.success ? undefined : res.message);
    } catch (e) {
      setRow(name, 'idle', errMsg(e, 'Could not load this integration.'));
    }
  };

  const handleDisconnect = async (name: string) => {
    setRow(name, 'disconnecting');
    try {
      await integrationsApi.disconnect(name);
      await syncRow(name);
    } catch (e) {
      setRow(name, 'idle', errMsg(e, 'Disconnect failed.'));
    }
  };

  const handleToggle = async (name: string, enabled: boolean) => {
    setIntegrations((prev) =>
      prev.map((i) => (i.name === name ? { ...i, enabled } : i)),
    );
    setToggleStates((prev) => ({ ...prev, [name]: { toggling: true } }));
    try {
      await integrationsApi.toggle(name, enabled);
      setToggleStates((prev) => ({ ...prev, [name]: { toggling: false } }));
    } catch (e) {
      setIntegrations((prev) =>
        prev.map((i) => (i.name === name ? { ...i, enabled: !enabled } : i)),
      );
      setToggleStates((prev) => ({
        ...prev,
        [name]: { toggling: false, error: errMsg(e, 'Could not update plugin.') },
      }));
    }
  };

  const handleBuiltInReconnect = async (name: string, providerOverride?: string) => {
    const item = integrations.find((i) => i.name === name);
    if (!item) return;

    if (item.auth_type === 'composio') {
      void connectViaPopup(name, `Reconnect ${item.display_name}`, 'list');
      return;
    }

    const provider = providerOverride ?? item.auth_type ?? connectionIds(item)[0];
    if (!provider) return;

    if (provider === 'macos') {
      setRow(name, 'connecting', undefined, provider);
      try {
        const result = await integrationsApi.authorizeMacosCalendar();
        await syncRow(name, result.success ? undefined : result.message);
      } catch (e) {
        setRow(name, 'idle', errMsg(e, 'Could not request Calendar access on this Mac.'));
      }
      return;
    }

    setRow(name, 'connecting', undefined, provider);
    try {
      const { authorize_url } = await oauthApi.authorize(provider, window.location.origin, {
        plugin: name === provider ? undefined : name,
      });
      const launch = await beginOAuthAuthorization(`Reconnect ${item.display_name}`, authorize_url);
      closeOAuthPopup(popupRef.current);
      clearPopup();
      popupRef.current = launch.popup ?? null;
      activeConnectTarget.current = { source: 'list', name };
      popupCleanupRef.current = watchOAuthCompletion({
        app: provider,
        mode: launch.mode,
        popup: launch.popup,
        checkComplete: () => oauthApi.getProviderStatus(provider).then((s) => s.connected),
        onComplete: (msg) => {
          clearPopup();
          activeConnectTarget.current = null;
          void loadProviderStatuses();
          void syncRow(name, msg.success ? undefined : 'Authorization failed.');
        },
        onAborted: () => {
          clearPopup();
          activeConnectTarget.current = null;
          void syncRow(
            name,
            launch.mode === 'external'
              ? 'Authorization timed out. Finish sign-in in your browser, then try again.'
              : 'Auth window closed before finishing.',
          );
        },
      });
    } catch (e) {
      if (e instanceof OAuthApiError && e.status === 409) {
        setSetupTarget({ name, provider });
        setSetupError(undefined);
        setRow(name, 'idle');
        return;
      }
      setRow(name, 'idle', errMsg(e, 'Could not start reconnect.'));
    }
  };

  const handleBuiltInDisconnect = async (name: string, provider?: string) => {
    setRow(name, 'disconnecting', undefined, provider);
    try {
      if (provider === 'macos') {
        await syncRow(
          name,
          'Turn off Calendar access in System Settings → Privacy & Security → Calendars.',
        );
        return;
      }
      if (provider) {
        await oauthApi.deleteProvider(provider);
        await loadProviderStatuses();
      } else {
        await integrationsApi.disconnect(name);
      }
      await syncRow(name);
    } catch (e) {
      setRow(name, 'idle', errMsg(e, 'Disconnect failed.'));
    }
  };

  const handleSaveProviderSetup = async (clientId: string, clientSecret?: string) => {
    if (!setupTarget) return;
    const { name, provider } = setupTarget;
    setSetupError(undefined);
    setRow(name, 'connecting', undefined, provider);
    try {
      await oauthApi.configure(provider, clientId, clientSecret);
      await loadProviderStatuses();
      setSetupTarget(null);
      await handleBuiltInReconnect(name, provider);
    } catch (e) {
      setRow(name, 'idle');
      setSetupError(errMsg(e, `Could not save ${connectionLabel(provider)} credentials.`));
    }
  };

  const builtIn = integrations.filter((i) => i.kind === 'built_in');
  const connected = integrations.filter((i) => i.kind === 'composio' && i.connected);
  const available = integrations.filter((i) => i.kind === 'composio' && !i.connected);

  const knownNames = new Set(integrations.map((i) => i.name));
  const listQueryLower = listQuery.trim().toLowerCase();
  const catalogQueryLower = catalogQuery.trim().toLowerCase();

  const filteredBuiltIn = builtIn.filter((i) =>
    matchesQuery(`${i.display_name} ${i.description}`, listQueryLower),
  );
  const filteredConnected = connected.filter((i) =>
    matchesQuery(`${i.display_name} ${i.description}`, listQueryLower),
  );
  const filteredAvailable = available.filter((i) =>
    matchesQuery(`${i.display_name} ${i.description}`, catalogQueryLower),
  );

  const catalogResults = catalogItems.filter((c) => !knownNames.has(c.slug));
  const myAppsCount = builtIn.length + connected.length;
  const discoverCount = available.length + (catalogLoaded ? catalogResults.length : 0);
  const selectedItem = integrations.find((item) => item.name === selectedName) ?? null;
  const showDetailMobile = Boolean(selectedItem && activeTab === 'my_apps');

  const builtInSubtitle = (item: IntegrationSummary) => {
    const summary = connectionSummary(item, providerStatuses);
    if (summary) return summary;
    if (item.status === 'error') return 'Needs connection';
    if (item.enabled) return `${item.tool_count} tool${item.tool_count !== 1 ? 's' : ''} active`;
    return 'Disabled';
  };

  return (
    <>
      <StatusBarWorkspaceHeader
        title="Apps"
        titleId="integ-title"
        subtitle="Manage connected tools and discover new capabilities."
        onClose={closeOverlay}
        closeLabel="Close apps"
        trailing={
          <SegmentedTabs
            idPrefix="apps"
            label="App sections"
            value={activeTab}
            onChange={(tab) => {
              setActiveTab(tab);
              if (tab === 'discover') setSelectedName(null);
            }}
            className="order-3 w-full justify-center sm:order-none sm:w-auto"
            tabs={[
              { value: 'my_apps', label: 'My Apps', count: myAppsCount },
              { value: 'discover', label: 'Discover', count: discoverCount || undefined },
            ]}
          />
        }
      />

      <div
        id={`apps-panel-${activeTab}`}
        role="tabpanel"
        aria-labelledby={`apps-tab-${activeTab}`}
        className="flex min-h-0 flex-1 flex-col"
      >
        {loading ? (
          <div className="py-2">
            <SkeletonRow delay={0} />
            <SkeletonRow delay={80} />
            <SkeletonRow delay={160} />
          </div>
        ) : fetchError ? (
          <div className="flex flex-col gap-4 px-6 py-8">
            <div className="flex items-start gap-3">
              <WarningIcon size={16} className="mt-0.5 flex-shrink-0 text-status-danger/70" />
              <p className="type-body text-foreground-muted">{fetchError}</p>
            </div>
            <Button
              variant="ghost"
              color="brand"
              size="sm"
              className="self-start"
              onClick={() => void fetchList()}
            >
              Retry
            </Button>
          </div>
        ) : activeTab === 'my_apps' ? (
          <div className="grid min-h-0 flex-1 md:grid-cols-[minmax(260px,0.9fr)_minmax(360px,1.1fr)]">
            <div
              className={cn(
                'flex min-h-0 flex-col border-outline/15 md:border-r',
                showDetailMobile && 'hidden md:flex',
              )}
            >
              <div className="shrink-0 px-4 pb-3 pt-3">
                <SearchField
                  id="my-apps-search"
                  label="Search my apps"
                  labelHidden
                  value={listQuery}
                  onChange={setListQuery}
                  placeholder="Filter installed apps…"
                />
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto pb-4 scrollbar-thin">
                {filteredBuiltIn.length === 0 && filteredConnected.length === 0 ? (
                  <EmptyState
                    className="m-4"
                    title={listQuery ? 'No matching apps' : 'No apps yet'}
                    description={
                      listQuery
                        ? `Nothing matched “${listQuery}”.`
                        : 'Connect a service from Discover to get started.'
                    }
                    action={
                      !listQuery ? (
                        <Button size="sm" onClick={() => setActiveTab('discover')}>
                          Browse Discover
                        </Button>
                      ) : undefined
                    }
                  />
                ) : (
                  <>
                    {filteredConnected.length > 0 && (
                      <section>
                        <SectionLabel
                          icon={<CheckCircleIcon size={12} weight="fill" className="text-status-success" />}
                          label="Connected"
                          count={filteredConnected.length}
                        />
                        {filteredConnected.map((item) => {
                          const row = getRow(item.name);
                          const busy = ['connecting', 'disconnecting', 'reconciling'].includes(row.phase);
                          return (
                            <AppListRow
                              key={item.name}
                              item={item}
                              selected={selectedName === item.name}
                              busy={busy}
                              error={row.error}
                              subtitle={metaText(item)}
                              onSelect={setSelectedName}
                            />
                          );
                        })}
                      </section>
                    )}
                    {filteredBuiltIn.length > 0 && (
                      <section>
                        <SectionLabel
                          icon={<CpuIcon size={12} weight="bold" className="text-foreground-subtle" />}
                          label="Built-in"
                          count={filteredBuiltIn.length}
                        />
                        {filteredBuiltIn.map((item) => {
                          const row = getRow(item.name);
                          const busy = ['connecting', 'disconnecting', 'reconciling'].includes(row.phase)
                            || (toggleStates[item.name]?.toggling ?? false);
                          return (
                            <AppListRow
                              key={item.name}
                              item={item}
                              selected={selectedName === item.name}
                              busy={busy}
                              error={toggleStates[item.name]?.error ?? row.error}
                              subtitle={builtInSubtitle(item)}
                              onSelect={setSelectedName}
                            />
                          );
                        })}
                      </section>
                    )}
                  </>
                )}
              </div>
            </div>

            <aside
              className={cn(
                'min-h-0 overflow-y-auto scrollbar-thin',
                !showDetailMobile && 'hidden md:block',
              )}
              aria-label="App details"
            >
              {!selectedItem ? (
                <EmptyState
                  className="m-5"
                  icon={<ShieldCheckIcon size={20} />}
                  title="Select an app"
                  description="Check connection status, manage sources, or see what JARV1S can do."
                />
              ) : (
                <>
                  <div className="px-6 pt-4 md:hidden">
                    <Button
                      variant="ghost"
                      color="action"
                      size="sm"
                      icon={<ArrowLeftIcon size={14} />}
                      onClick={() => setSelectedName(null)}
                    >
                      Back to apps
                    </Button>
                  </div>
                  <AppDetail
                    item={selectedItem}
                    state={getRow(selectedItem.name)}
                    toggling={toggleStates[selectedItem.name]?.toggling ?? false}
                    providerStatuses={providerStatuses}
                    onToggle={handleToggle}
                    onConnect={selectedItem.kind === 'built_in' ? handleBuiltInReconnect : handleConnect}
                    onRecover={handleRecover}
                    onAuthorizeConnection={(name, connectionId) => void handleBuiltInReconnect(name, connectionId)}
                    onSetupConnection={(name, connectionId) => {
                      setSetupTarget({ name, provider: connectionId });
                      setSetupError(undefined);
                    }}
                    onConfirmDisconnect={(name, provider) => setRow(name, 'confirm_disconnect', undefined, provider)}
                    onDisconnect={selectedItem.kind === 'built_in' ? handleBuiltInDisconnect : handleDisconnect}
                    onCancelDisconnect={(name) => setRow(name, 'idle')}
                  >
                    {setupTarget?.name === selectedItem.name && (
                      <ProviderSetupPanel
                        provider={setupTarget.provider}
                        busy={getRow(setupTarget.name).phase === 'connecting'}
                        error={setupError}
                        onSave={(clientId, clientSecret) => void handleSaveProviderSetup(clientId, clientSecret)}
                        onCancel={() => {
                          setSetupTarget(null);
                          setSetupError(undefined);
                          setRow(selectedItem.name, 'idle');
                        }}
                      />
                    )}
                  </AppDetail>
                </>
              )}
            </aside>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="shrink-0 space-y-2 px-6 pb-3 pt-3">
              <SearchField
                inputRef={searchInputRef}
                id="app-search"
                label="Search available apps"
                value={catalogQuery}
                onChange={handleCatalogQueryChange}
                placeholder="Calendar, messaging, music…"
              />
              {catalogLoading && (
                <div className="flex items-center gap-2 text-xs text-foreground-subtle" role="status">
                  <SpinnerIcon size={13} className="animate-spin text-brand" />
                  {catalogQuery.trim() ? 'Searching catalog' : 'Loading catalog'}
                </div>
              )}
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-5 scrollbar-thin">
              {catalogError && (
                <EmptyState
                  className="mb-4"
                  tone="error"
                  icon={<WarningIcon size={18} />}
                  title="Catalog unavailable"
                  description={catalogError}
                  action={
                    <Button size="sm" onClick={() => void searchCatalog(catalogQuery.trim())}>
                      Retry
                    </Button>
                  }
                />
              )}

              {!catalogError && filteredAvailable.length === 0 && catalogResults.length === 0 && !catalogLoading ? (
                <EmptyState
                  icon={catalogQuery.trim() ? <MagnifyingGlassIcon size={18} /> : <LinkIcon size={18} />}
                  title={catalogQuery.trim() ? `No apps found for “${catalogQuery}”` : 'No apps to discover'}
                  description={
                    catalogQuery.trim()
                      ? 'Try a broader keyword like calendar, github, or slack.'
                      : 'Suggested services will appear here when the catalog is available.'
                  }
                />
              ) : (
                <div className="space-y-6">
                  {filteredAvailable.length > 0 && (
                    <section>
                      <SectionLabel
                        icon={<PlugIcon size={12} className="text-foreground-subtle" />}
                        label="Ready to connect"
                        count={filteredAvailable.length}
                      />
                      <div className="mt-2 grid gap-3 sm:grid-cols-2">
                        {filteredAvailable.map((item) => (
                          <article
                            key={item.name}
                            className="flex min-h-36 flex-col gap-3 rounded-panel border border-outline/15 bg-surface/10 p-4"
                          >
                            <div className="min-w-0">
                              <h3 className="truncate type-heading text-foreground">
                                {item.display_name}
                              </h3>
                              <p className="mt-1 line-clamp-3 type-body text-foreground-muted">
                                {item.description || metaText(item)}
                              </p>
                            </div>
                            <div className="mt-auto flex min-h-10 items-center justify-end gap-2">
                              <Button
                                size="sm"
                                variant="ghost"
                                color="action"
                                shape="control"
                                onClick={() => {
                                  setSelectedName(item.name);
                                  setActiveTab('my_apps');
                                }}
                              >
                                Details
                              </Button>
                              <Button
                                size="sm"
                                variant="ghost"
                                color="brand"
                                shape="control"
                                disabled={['connecting', 'disconnecting', 'reconciling'].includes(getRow(item.name).phase)}
                                onClick={() => handleConnect(item.name)}
                              >
                                Connect
                              </Button>
                            </div>
                          </article>
                        ))}
                      </div>
                    </section>
                  )}

                  {catalogResults.length > 0 && (
                    <section>
                      <SectionLabel
                        icon={<MagnifyingGlassIcon size={12} className="text-foreground-subtle" />}
                        label={catalogQuery.trim() ? 'Catalog results' : 'Browse catalog'}
                        count={catalogResults.length}
                      />
                      <div className="mt-2 grid gap-3 sm:grid-cols-2">
                        {catalogResults.map((item) => (
                          <CatalogCard
                            key={item.slug}
                            item={item}
                            phase={catalogRowStates[item.slug]?.phase ?? 'idle'}
                            error={catalogRowStates[item.slug]?.error}
                            onConnect={handleCatalogConnect}
                          />
                        ))}
                      </div>
                    </section>
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </>
  );
};
