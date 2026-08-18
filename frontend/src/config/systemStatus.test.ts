import { describe, expect, it } from 'vitest'
import { getSystemStatus, resolveDashboardStatus } from './systemStatus'
import type { AgentState, AttentionMode, ConnectionState } from '../types'
import type { HostState } from '../store/useJarvisStore'

const base = {
  hostState: 'online' as HostState,
  connectionState: 'connected' as ConnectionState,
  agentState: 'idle' as AgentState,
  reconnectAttempt: 0,
  isSpeaking: false,
  isAudioContextReady: false,
  isMuted: false,
  softMuted: false,
  attentionMode: 'active' as AttentionMode,
}

describe('resolveDashboardStatus', () => {
  it('keeps host and connection recovery dominant', () => {
    expect(resolveDashboardStatus({
      ...base,
      hostState: 'offline',
      agentState: 'listening',
      isSpeaking: true,
    }).label).toBe('Host offline')

    expect(resolveDashboardStatus({
      ...base,
      connectionState: 'reconnecting',
      reconnectAttempt: 2,
      isMuted: true,
    })).toMatchObject({
      label: 'Reconnecting (2)…',
      hologramColor: 'inactive',
      pulse: true,
    })
  })

  it('keeps local playback authoritative over backend work state', () => {
    expect(resolveDashboardStatus({
      ...base,
      agentState: 'running_tool',
      isSpeaking: true,
    })).toMatchObject({
      label: 'Speaking',
      pulse: true,
    })
  })

  it('surfaces active agent phases as sentence-case status', () => {
    expect(resolveDashboardStatus({ ...base, agentState: 'listening' }).label).toBe('Listening')
    expect(resolveDashboardStatus({ ...base, agentState: 'thinking' }).label).toBe('Thinking')
    expect(resolveDashboardStatus({ ...base, agentState: 'composing_tool' }).label).toBe('Thinking')
    expect(resolveDashboardStatus({ ...base, agentState: 'running_tool' }).label).toBe('Working')
    expect(resolveDashboardStatus({ ...base, agentState: 'transcribing' }).label).toBe('Transcribing')
    expect(resolveDashboardStatus({ ...base, agentState: 'waking' }).label).toBe('Detected')
    expect(resolveDashboardStatus({ ...base, agentState: 'speaking' }).label).toBe('Speaking')
  })

  it('prefers active phases over privacy chrome', () => {
    expect(resolveDashboardStatus({
      ...base,
      agentState: 'listening',
      isMuted: true,
      attentionMode: 'paused',
    }).label).toBe('Listening')
  })

  it('orders idle privacy and attention states', () => {
    expect(resolveDashboardStatus({
      ...base,
      attentionMode: 'paused',
      isMuted: true,
      softMuted: true,
    })).toMatchObject({
      label: 'Paused',
      hologramColor: 'error',
    })

    expect(resolveDashboardStatus({
      ...base,
      isMuted: true,
      softMuted: true,
      attentionMode: 'quiet',
    })).toMatchObject({
      label: 'Mic muted',
      hologramColor: 'warning',
    })

    expect(resolveDashboardStatus({
      ...base,
      softMuted: true,
      attentionMode: 'quiet',
    })).toMatchObject({
      label: 'Voice muted',
      hologramColor: 'warning',
    })

    expect(resolveDashboardStatus({
      ...base,
      attentionMode: 'quiet',
      isAudioContextReady: true,
    })).toMatchObject({
      label: 'Quiet mode',
      hologramColor: 'warning',
    })
  })

  it('distinguishes ready from voice-active when idle', () => {
    expect(resolveDashboardStatus(base)).toMatchObject({
      label: 'Ready',
      hologramColor: 'default',
    })

    expect(resolveDashboardStatus({
      ...base,
      isAudioContextReady: true,
    })).toMatchObject({
      label: 'Voice active',
      hologramColor: 'default',
    })
  })

  it('surfaces mic stall instead of voice-active when PCM is dead', () => {
    expect(resolveDashboardStatus({
      ...base,
      isAudioContextReady: true,
      captureStalled: true,
    })).toMatchObject({
      label: 'Mic stalled',
      hologramColor: 'warning',
    })

    expect(resolveDashboardStatus({
      ...base,
      agentState: 'listening',
      isAudioContextReady: true,
      captureStalled: true,
    }).label).toBe('Mic stalled')
  })
})

describe('getSystemStatus', () => {
  it('exposes sentence-case recovery labels for the control dock', () => {
    expect(getSystemStatus('online', 'disconnected', 'idle').shortLabel).toBe('Connect')
    expect(getSystemStatus('offline', 'connected', 'idle').shortLabel).toBe('Retry host')
  })
})
