import { ConnectionState, AgentState, AttentionMode } from '../types';
import type { HostState } from '../store/useJarvisStore';

export interface StatusConfig {
  color: string;      // Tailwind bg class for LED/indicators
  label: string;      // Human readable text
  shortLabel?: string; // Shorter version for buttons
  hologramColor: 'default' | 'error' | 'inactive'; // Variant for Hologram component
  buttonColor?: 'brand' | 'critical' | 'subtle' | 'neutral'; // Variant for Button component
  iconClass?: string;
  pulse?: boolean;
}

export type DashboardHologramColor = 'default' | 'warning' | 'error' | 'inactive';

export interface DashboardStatus {
  label: string;
  hoverLabel: string | null;
  color: string;
  hologramColor: DashboardHologramColor;
  pulse: boolean;
  iconClass?: string;
  /** Transport / host recovery still owns the ControlBar primary action. */
  recovery: StatusConfig | null;
}

/**
 * Transport and agent chrome for ControlBar recovery actions.
 * The trust pill uses `resolveDashboardStatus` for readable status copy.
 */
export const getSystemStatus = (
  host: HostState,
  connection: ConnectionState,
  agent: AgentState,
  retryCount: number = 0
): StatusConfig => {
  if (host === 'offline') {
    return {
      color: 'bg-status-danger shadow-glow-danger',
      label: 'Host offline',
      shortLabel: 'Retry host',
      hologramColor: 'error',
      buttonColor: 'critical'
    };
  }

  // 1. Connection Critical States (Override Agent State)
  if (connection === 'disconnected') {
    return {
      color: 'bg-foreground-disabled opacity-40',
      label: 'Disconnected',
      shortLabel: 'Connect',
      hologramColor: 'inactive',
      buttonColor: 'subtle'
    };
  }

  if (connection === 'reconnecting') {
    return {
      color: 'bg-hologram-inactive',
      label: `Reconnecting (${retryCount})…`,
      shortLabel: 'Retrying…',
      hologramColor: 'inactive',
      buttonColor: 'subtle',
      pulse: true
    };
  }

  if (connection === 'error') {
    return {
      color: 'bg-status-danger shadow-glow-danger',
      label: 'Connection error',
      shortLabel: 'Reconnect',
      hologramColor: 'error',
      buttonColor: 'critical'
    };
  }

  if (connection === 'connecting') {
    return {
      color: 'bg-brand opacity-50',
      label: 'Connecting…',
      shortLabel: 'Connecting…',
      hologramColor: 'inactive',
      buttonColor: 'subtle',
      pulse: true
    };
  }

  // 2. Connected Agent States
  switch (agent) {
    case 'waking':
      return {
        color: 'bg-brand shadow-glow-brand',
        label: 'Detected',
        hologramColor: 'default',
        buttonColor: 'brand',
        iconClass: 'animate-[pulse_0.5s_cubic-bezier(0.4,0,0.6,1)_infinite]'
      };
    case 'listening':
      return {
        color: 'bg-brand shadow-glow-brand',
        label: 'Listening',
        hologramColor: 'default',
        buttonColor: 'brand',
      };
    case 'thinking':
      return {
        color: 'bg-brand shadow-glow-brand',
        label: 'Thinking',
        hologramColor: 'default',
        buttonColor: 'brand',
        pulse: true
      };
    case 'composing_tool':
      return {
        color: 'bg-brand shadow-glow-brand',
        label: 'Thinking',
        hologramColor: 'default',
        buttonColor: 'brand',
        pulse: true
      };
    case 'running_tool':
      return {
        color: 'bg-brand shadow-glow-brand',
        label: 'Working',
        hologramColor: 'default',
        buttonColor: 'brand',
        pulse: true
      };
    case 'speaking':
      return {
        color: 'bg-brand-output shadow-glow-output',
        label: 'Speaking',
        hologramColor: 'default',
        buttonColor: 'brand',
        pulse: true
      };
    case 'transcribing':
      return {
        color: 'bg-brand opacity-80',
        label: 'Transcribing',
        hologramColor: 'default',
        buttonColor: 'brand',
        pulse: true
      };
    case 'idle':
    default:
      return {
        color: 'bg-brand-output opacity-50',
        label: 'Ready',
        hologramColor: 'default',
        buttonColor: 'brand'
      };
  }
};

const PHASE_HOVER: Partial<Record<AgentState, string>> = {
  waking: 'Wake word detected',
  listening: 'Listening for your request',
  transcribing: 'Turning speech into text',
  thinking: 'Working on a response',
  composing_tool: 'Working on a response',
  running_tool: 'Running a tool',
  speaking: 'Speaking a reply',
};

/**
 * Trust-pill status: answers “What is JARV1S doing, and can I interact?”
 *
 * Precedence: host/connection recovery → local playback / active agent phase →
 * privacy & attention → idle readiness. Local `isSpeaking` outranks backend
 * agent phase, matching live-stage playback authority.
 */
export const resolveDashboardStatus = (input: {
  hostState: HostState;
  connectionState: ConnectionState;
  agentState: AgentState;
  reconnectAttempt?: number;
  isSpeaking: boolean;
  isAudioContextReady: boolean;
  isMuted: boolean;
  softMuted: boolean;
  attentionMode: AttentionMode;
  /** Mic pipeline claimed but PCM not flowing. */
  captureStalled?: boolean;
}): DashboardStatus => {
  const {
    hostState,
    connectionState,
    agentState,
    reconnectAttempt = 0,
    isSpeaking,
    isAudioContextReady,
    isMuted,
    softMuted,
    attentionMode,
    captureStalled = false,
  } = input;

  const transport = getSystemStatus(hostState, connectionState, agentState, reconnectAttempt);
  const connected = hostState !== 'offline' && connectionState === 'connected';

  if (!connected) {
    return {
      label: transport.label,
      hoverLabel: transport.shortLabel ?? transport.label,
      color: transport.color,
      hologramColor: transport.hologramColor,
      pulse: Boolean(transport.pulse),
      iconClass: transport.iconClass,
      recovery: transport,
    };
  }

  // Rendered audio is authoritative while playback drains.
  if (isSpeaking || agentState === 'speaking') {
    return {
      label: 'Speaking',
      hoverLabel: PHASE_HOVER.speaking ?? null,
      color: 'bg-brand-output shadow-glow-output',
      hologramColor: 'default',
      pulse: true,
      recovery: null,
    };
  }

  if (
    isAudioContextReady
    && captureStalled
    && !isMuted
    && agentState === 'listening'
  ) {
    return {
      label: 'Mic stalled',
      hoverLabel: 'Microphone audio stopped · focus the app to recover',
      color: 'bg-status-warning',
      hologramColor: 'warning',
      pulse: false,
      recovery: null,
    };
  }

  if (agentState !== 'idle') {
    const phase = getSystemStatus(hostState, connectionState, agentState, reconnectAttempt);
    return {
      label: phase.label,
      hoverLabel: PHASE_HOVER[agentState] ?? phase.label,
      color: phase.color,
      hologramColor: 'default',
      pulse: Boolean(phase.pulse),
      iconClass: phase.iconClass,
      recovery: null,
    };
  }

  // Idle: privacy and attention before quiet readiness.
  if (attentionMode === 'paused') {
    return {
      label: 'Paused',
      hoverLabel: 'Paused · say power on',
      color: 'bg-status-danger shadow-glow-danger',
      hologramColor: 'error',
      pulse: false,
      recovery: null,
    };
  }

  if (isMuted) {
    return {
      label: 'Mic muted',
      hoverLabel: 'Microphone muted on this device',
      color: 'bg-status-warning',
      hologramColor: 'warning',
      pulse: false,
      recovery: null,
    };
  }

  if (softMuted) {
    return {
      label: 'Voice muted',
      hoverLabel: 'Muted here · say wake up',
      color: 'bg-status-warning',
      hologramColor: 'warning',
      pulse: false,
      recovery: null,
    };
  }

  if (attentionMode === 'quiet') {
    return {
      label: 'Quiet mode',
      hoverLabel: 'Quiet · urgent only',
      color: 'bg-status-warning',
      hologramColor: 'warning',
      pulse: false,
      recovery: null,
    };
  }

  if (isAudioContextReady && captureStalled) {
    return {
      label: 'Mic stalled',
      hoverLabel: 'Microphone audio stopped · focus the app to recover',
      color: 'bg-status-warning',
      hologramColor: 'warning',
      pulse: false,
      recovery: null,
    };
  }

  if (isAudioContextReady) {
    return {
      label: 'Voice active',
      hoverLabel: 'Microphone ready',
      color: 'bg-brand-output opacity-50',
      hologramColor: 'default',
      pulse: false,
      recovery: null,
    };
  }

  return {
    label: 'Ready',
    hoverLabel: 'Connected',
    color: 'bg-brand-output opacity-50',
    hologramColor: 'default',
    pulse: false,
    recovery: null,
  };
};
