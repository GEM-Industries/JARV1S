import React, { useEffect, useRef, useState } from 'react';
import { LinkIcon, CheckCircleIcon, XCircleIcon, ArrowSquareOutIcon } from '@phosphor-icons/react';
import { cn } from '../../../utils/cn';
import { Button } from '../../ui/Button';
import { integrationsApi } from '../../../client/integrationsApi';
import { WidgetDefinition, BaseWidgetProps } from './types';
import {
  beginOAuthAuthorization,
  closeOAuthPopup,
  watchOAuthCompletion,
} from '../../../utils/oauthFlow';

interface ConnectWidgetData {
  /** Integration slug used by backend reconciliation (e.g. "github") */
  integration_name: string;
  /** Display name for the integration (e.g. "Spotify", "GitHub") */
  app_name: string;
  /** The Composio Connect Link URL — user clicks to authorize */
  connect_url: string;
  /** Current connection status */
  status: 'pending' | 'connected' | 'error';
  /** Number of tools available once connected (shown after success) */
  tool_count?: number;
  /** Error message if status === 'error' */
  error?: string;
}

const ConnectHero: React.FC<ConnectWidgetData & BaseWidgetProps> = ({
  integration_name,
  app_name,
  connect_url,
  status,
  tool_count,
  error,
}) => {
  const [clicked, setClicked] = useState(false);
  const [statusOverride, setStatusOverride] = useState<ConnectWidgetData['status'] | null>(null);
  const [popupError, setPopupError] = useState<string | null>(null);
  const popupRef = useRef<Window | null>(null);
  const popupCleanupRef = useRef<(() => void) | null>(null);

  const clearPopupTracking = () => {
    popupCleanupRef.current?.();
    popupCleanupRef.current = null;
    popupRef.current = null;
  };

  useEffect(
    () => () => {
      closeOAuthPopup(popupRef.current);
      clearPopupTracking();
    },
    []
  );

  useEffect(() => {
    if (status === 'connected') {
      setStatusOverride('connected');
      setPopupError(null);
      setClicked(false);
      return;
    }
    if (status === 'error') {
      setStatusOverride('error');
      setClicked(false);
    }
  }, [status]);

  const reconcileConnection = async (fallbackError: string) => {
    try {
      const result = await integrationsApi.reconcile(integration_name);
      if (result.success) {
        setStatusOverride('connected');
        setPopupError(null);
      } else {
        setStatusOverride('error');
        setPopupError(result.message || fallbackError);
      }
    } catch (reconcileError) {
      setStatusOverride('error');
      setPopupError(
        reconcileError instanceof Error ? reconcileError.message : fallbackError
      );
    } finally {
      setClicked(false);
    }
  };

  const handleConnect = async () => {
    setClicked(true);
    setStatusOverride(null);
    setPopupError(null);

    try {
      const launch = await beginOAuthAuthorization(`Connect ${app_name}`, connect_url);
      popupRef.current = launch.popup ?? null;

      popupCleanupRef.current = watchOAuthCompletion({
        app: integration_name,
        mode: launch.mode,
        popup: launch.popup,
        checkComplete: () => integrationsApi.reconcile(integration_name).then((r) => r.success),
        onComplete: (message) => {
          clearPopupTracking();
          if (!message.success) {
            setStatusOverride('error');
            setPopupError('Authorization failed.');
            setClicked(false);
            return;
          }
          void reconcileConnection(
            `${app_name} connected, but its tools are not ready yet.`
          );
        },
        onAborted: () => {
          clearPopupTracking();
          void reconcileConnection(
            launch.mode === 'external'
              ? 'Authorization timed out. Finish sign-in in your browser, then try again.'
              : 'Authorization window closed before the connection finished.'
          );
        },
      });
    } catch (error) {
      clearPopupTracking();
      setClicked(false);
      setPopupError(error instanceof Error ? error.message : 'Could not open the authorization page.');
    }
  };

  const effectiveStatus = statusOverride ?? status;
  const isPending = effectiveStatus === 'pending';
  const isConnected = effectiveStatus === 'connected';
  const isError = effectiveStatus === 'error';

  return (
    <div className="flex flex-col h-full px-5 py-5 select-none">
      {/* Header */}
      <div className="flex items-center gap-3 mb-4 shrink-0">
        <LinkIcon
          size={18}
          weight="fill"
          className={cn(
            isPending ? 'text-brand' :
            isConnected ? 'text-status-success' :
            'text-status-danger'
          )}
        />
        <span className="type-heading text-foreground">
          Connect {app_name}
        </span>
        {isConnected && (
          <span className="ml-auto type-label-small text-status-success">
            Connected
          </span>
        )}
        {isError && (
          <span className="ml-auto type-label-small text-status-danger">
            Failed
          </span>
        )}
      </div>

      {/* Body */}
      {isPending && (
        <>
          <p className="type-body-reading text-foreground mb-2">
            Authorize {app_name} to get started.
          </p>
          <p className="type-body text-foreground-muted mb-4">
            Click the button below to open the authorization page.
            Once you approve, {app_name} tools will be available immediately.
          </p>
        </>
      )}

      {isConnected && (
        <div className="flex items-center gap-3 mb-4">
          <CheckCircleIcon size={32} weight="fill" className="text-status-success shrink-0" />
          <div>
            <p className="font-body text-base text-foreground leading-snug">
              {app_name} is connected.
            </p>
            {tool_count !== undefined && (
              <p className="font-body text-sm text-foreground-muted">
                {tool_count} tool{tool_count !== 1 ? 's' : ''} available via{' '}
                <span className="font-mono">jarvis.{app_name.toLowerCase()}.*</span>
              </p>
            )}
          </div>
        </div>
      )}

      {isError && (
        <div className="flex items-start gap-3 mb-4">
          <XCircleIcon size={20} weight="fill" className="text-status-danger shrink-0 mt-0.5" />
          <p className="font-body text-sm text-foreground-muted leading-relaxed">
            {error || `Could not connect ${app_name}. Try again or check your Composio settings.`}
          </p>
        </div>
      )}

      {popupError && (
        <div className="flex items-start gap-3 mb-4">
          <XCircleIcon size={20} weight="fill" className="text-status-danger shrink-0 mt-0.5" />
          <p className="font-body text-sm text-foreground-muted leading-relaxed">
            {popupError}
          </p>
        </div>
      )}

      {isPending && (
        <Button
          variant="ghost"
          color="brand"
          size="sm"
          onClick={handleConnect}
          disabled={clicked}
          className="mt-auto w-full"
          icon={<ArrowSquareOutIcon size={14} weight="bold" />}
        >
          {clicked ? `Waiting for ${app_name}\u2026` : `Authorize ${app_name}`}
        </Button>
      )}
    </div>
  );
};

export const ConnectWidget: WidgetDefinition<ConnectWidgetData> = {
  Hero: ConnectHero,
  getCompressedConfig: (data) => ({
    icon: (
      <LinkIcon
        size={20}
        weight="fill"
        className={cn(
          data.status === 'connected' ? 'text-status-success' :
          data.status === 'error' ? 'text-status-danger' :
          'text-brand'
        )}
      />
    ),
    label: data.status === 'connected' ? 'Connected' : data.status === 'error' ? 'Failed' : 'Auth',
    labelVariant: 'mono',
    width: 'wide',
  }),
};
