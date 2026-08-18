import { useJarvisStore } from '../store/useJarvisStore'
import { WSMessage, WSResponse } from '../types'
import { err, ok, Result } from '../utils/result'
import { refreshHostState } from '../runtime/hostLifecycle'
import {
  getClientDiagnosticsRecorder,
  type ClientDiagnosticBatch,
} from './clientDiagnostics'

const NODE_REPLACED_CLOSE_CODE = 4001
const DEVICE_REVOKED_CLOSE_CODE = 4002

type RealtimeConnectionConfig = {
  buildUrl: () => string | Promise<string>
  onOpen: () => void
  onMessage: (message: WSResponse) => void
  onAuthRequired?: () => void
}

export class RealtimeConnection {
  private socket: WebSocket | null = null
  private reconnectTimeout: number | null = null
  private reconnectAttempts = 0
  private socketSerial = 0
  private pingInterval: number | null = null
  private lastPingTime = 0
  private awaitingPong = false
  private missedPongs = 0
  private lifecycleListenersBound = false
  private autoReconnect = false
  private connectInFlight = false
  private lastResumeProbeAt = 0
  private config: RealtimeConnectionConfig | null = null
  private readonly diagnostics = getClientDiagnosticsRecorder()
  private readonly RECONNECT_DELAY = 3000
  private readonly MAX_RECONNECT_DELAY = 30000
  private readonly HEARTBEAT_INTERVAL_MS = 5000
  private readonly MAX_MISSED_PONGS = 3

  configure(config: RealtimeConnectionConfig): void {
    this.config = config
    this.diagnostics.configure((batch) => this.sendDiagnosticBatch(batch))
  }

  connect(isRetry = false): void {
    this.autoReconnect = true
    this.bindLifecycleListeners()
    void this.connectAsync(isRetry)
  }

  private async connectAsync(isRetry = false): Promise<void> {
    if (!this.config) return
    if (this.connectInFlight) return
    if (this.socket?.readyState === WebSocket.OPEN || this.socket?.readyState === WebSocket.CONNECTING) {
      return
    }

    if (!isRetry) {
      this.reconnectAttempts = 0
      useJarvisStore.getState().setReconnectAttempt(0)
    }

    const socketId = ++this.socketSerial
    let url: string
    this.connectInFlight = true
    try {
      url = await this.config.buildUrl()
    } catch (error) {
      void refreshHostState()
      if ((error as Error).message === 'pairing_required') {
        this.autoReconnect = false
        this.unbindLifecycleListeners()
        this.config.onAuthRequired?.()
        useJarvisStore.getState().setConnectionState('disconnected')
        this.recordTransport('auth_required', {
          severity: 'warning',
          socketId,
          recovery: 'pairing',
        })
        return
      }
      useJarvisStore.getState().setConnectionState('error')
      this.recordTransport('connect_failed', {
        severity: 'warning',
        socketId,
        recovery: 'retry',
      })
      this.scheduleReconnect()
      return
    } finally {
      this.connectInFlight = false
    }
    if (!this.autoReconnect) return
    useJarvisStore.getState().setConnectionState('connecting')

    const socket = new WebSocket(url)
    this.socket = socket

    socket.onopen = () => {
      if (this.socket !== socket) return
      const recovered = this.reconnectAttempts > 0 || isRetry
      this.reconnectAttempts = 0
      useJarvisStore.getState().setReconnectAttempt(0)
      useJarvisStore.getState().setConnectionState('connected')
      useJarvisStore.getState().setHostState('online')
      this.startVisibleHeartbeat()
      this.recordTransport(recovered ? 'recovered' : 'open', {
        socketId,
        recovery: recovered ? 'reconnect' : 'initial',
      })
      this.diagnostics.flush()
      this.config?.onOpen()
    }

    socket.onmessage = (event) => {
      if (this.socket !== socket) return
      try {
        const parsed = JSON.parse(event.data) as WSResponse
        this.handleTransportMessage(parsed)
        this.config?.onMessage(parsed)
      } catch (e) {
        console.error('Failed to parse WS message', e)
      }
    }

    socket.onclose = (event) => {
      const isCurrent = this.socket === socket
      if (!isCurrent) return

      this.stopVisibleHeartbeat()
      void refreshHostState()
      if (event.code === NODE_REPLACED_CLOSE_CODE) {
        this.socket = null
        useJarvisStore.getState().setConnectionState('disconnected')
        this.recordTransport('closed', {
          severity: 'warning',
          socketId,
          code: event.code,
          clean: event.wasClean,
          recovery: 'node_replaced',
        })
        return
      }
      if (
        event.code === DEVICE_REVOKED_CLOSE_CODE
        || (event.code === 1008 && /invalid ticket|auth required|origin rejected/i.test(event.reason))
      ) {
        this.socket = null
        this.autoReconnect = false
        this.unbindLifecycleListeners()
        useJarvisStore.getState().setConnectionState('disconnected')
        this.recordTransport('closed', {
          severity: 'warning',
          socketId,
          code: event.code,
          clean: event.wasClean,
          recovery: 'auth_required',
        })
        this.config?.onAuthRequired?.()
        return
      }
      this.socket = null
      this.recordTransport('closed', {
        severity: event.wasClean ? 'info' : 'warning',
        socketId,
        code: event.code,
        clean: event.wasClean,
        recovery: this.autoReconnect ? 'retry' : 'stop',
      })
      this.scheduleReconnect()
    }

    socket.onerror = () => {
      if (this.socket !== socket) return
      useJarvisStore.getState().setConnectionState('error')
      void refreshHostState()
    }
  }

  disconnect(): void {
    this.autoReconnect = false
    this.stopVisibleHeartbeat()
    this.unbindLifecycleListeners()
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout)
      this.reconnectTimeout = null
    }

    const socket = this.socket
    this.socket = null
    socket?.close()
    useJarvisStore.getState().setConnectionState('disconnected')
  }

  send(message: WSMessage): Result<void> {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return err('WebSocket not connected')
    }
    this.socket.send(JSON.stringify(message))
    return ok(undefined)
  }

  isOpen(): boolean {
    return this.socket?.readyState === WebSocket.OPEN
  }

  private sendDiagnosticBatch(batch: ClientDiagnosticBatch): boolean {
    if (!this.isOpen()) return false
    const result = this.send({
      id: `diag-${Date.now()}`,
      type: 'client.diagnostics',
      data: {
        events: batch.events,
        dropped_count: batch.dropped_count,
      },
    })
    return result.ok
  }

  private recordTransport(
    phase: string,
    meta: {
      severity?: 'info' | 'warning' | 'error'
      socketId: number
      code?: number
      clean?: boolean
      recovery?: string
    },
  ): void {
    this.diagnostics.record('transport_transition', {
      severity: meta.severity ?? 'info',
      metadata: {
        phase,
        socket: meta.socketId,
        attempts: this.reconnectAttempts,
        code: meta.code ?? null,
        clean: meta.clean ?? null,
        recovery: meta.recovery ?? null,
      },
    })
  }

  private scheduleReconnect(): void {
    if (!this.autoReconnect) return
    if (this.reconnectTimeout) {
      return
    }

    const store = useJarvisStore.getState()
    this.reconnectAttempts++
    store.setReconnectAttempt(this.reconnectAttempts)
    store.setConnectionState('reconnecting')

    const delay = Math.min(this.RECONNECT_DELAY * this.reconnectAttempts, this.MAX_RECONNECT_DELAY)
    this.reconnectTimeout = window.setTimeout(() => {
      this.reconnectTimeout = null
      this.connect(true)
    }, delay)
  }

  private startVisibleHeartbeat(): void {
    this.stopVisibleHeartbeat()
    if (document.visibilityState !== 'visible' || !this.isOpen()) return

    this.sendHeartbeat()
    this.pingInterval = window.setInterval(() => {
      if (document.visibilityState !== 'visible') {
        this.stopVisibleHeartbeat()
        return
      }
      if (!this.isOpen()) return
      if (this.awaitingPong) {
        this.missedPongs += 1
      }
      if (this.missedPongs >= this.MAX_MISSED_PONGS) {
        this.recordTransport('heartbeat_timeout', {
          severity: 'warning',
          socketId: this.socketSerial,
          recovery: 'force_reconnect',
        })
        this.forceReconnect('visible heartbeat timeout')
        return
      }
      this.sendHeartbeat()
    }, this.HEARTBEAT_INTERVAL_MS)
  }

  private stopVisibleHeartbeat(): void {
    if (this.pingInterval) {
      clearInterval(this.pingInterval)
      this.pingInterval = null
    }
    this.awaitingPong = false
    this.missedPongs = 0
    useJarvisStore.getState().setSystemMetrics(null)
  }

  private sendHeartbeat(): void {
    if (!this.isOpen()) return
    this.lastPingTime = Date.now()
    this.awaitingPong = true
    this.send({
      id: `web-${Date.now()}`,
      type: 'system.ping',
      data: { timestamp: this.lastPingTime },
    })
  }

  private bindLifecycleListeners(): void {
    if (this.lifecycleListenersBound) return
    document.addEventListener('visibilitychange', this.handleVisibilityChange)
    window.addEventListener('focus', this.handleResume)
    window.addEventListener('pageshow', this.handleResume)
    window.addEventListener('online', this.handleResume)
    this.lifecycleListenersBound = true
  }

  private unbindLifecycleListeners(): void {
    if (!this.lifecycleListenersBound) return
    document.removeEventListener('visibilitychange', this.handleVisibilityChange)
    window.removeEventListener('focus', this.handleResume)
    window.removeEventListener('pageshow', this.handleResume)
    window.removeEventListener('online', this.handleResume)
    this.lifecycleListenersBound = false
  }

  private handleVisibilityChange = (): void => {
    if (document.visibilityState === 'visible') {
      this.handleResume()
    } else {
      this.stopVisibleHeartbeat()
    }
  }

  private handleResume = (): void => {
    if (!this.autoReconnect || document.visibilityState !== 'visible') return
    const now = Date.now()
    if (now - this.lastResumeProbeAt < 250) return
    this.lastResumeProbeAt = now
    void refreshHostState()
    if (this.isOpen()) {
      this.startVisibleHeartbeat()
      return
    }
    if (this.socket?.readyState === WebSocket.CONNECTING) return
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout)
      this.reconnectTimeout = null
    }
    this.connect(true)
  }

  private forceReconnect(reason: string): void {
    const socket = this.socket
    this.socket = null
    this.stopVisibleHeartbeat()
    socket?.close(4000, reason)
    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout)
      this.reconnectTimeout = null
    }
    this.connect(true)
  }

  private handleTransportMessage(message: WSResponse): void {
    if (message.type !== 'system.pong') return

    this.awaitingPong = false
    this.missedPongs = 0
    useJarvisStore.getState().setSystemMetrics(Date.now() - this.lastPingTime)
  }
}

export function getRealtimeConnection(): RealtimeConnection {
  const jarvisGlobal = globalThis as typeof globalThis & { __jarvisRealtimeConnection?: RealtimeConnection }
  if (!jarvisGlobal.__jarvisRealtimeConnection) {
    jarvisGlobal.__jarvisRealtimeConnection = new RealtimeConnection()
  }
  return jarvisGlobal.__jarvisRealtimeConnection
}

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    const jarvisGlobal = globalThis as typeof globalThis & { __jarvisRealtimeConnection?: RealtimeConnection }
    jarvisGlobal.__jarvisRealtimeConnection?.disconnect()
    delete jarvisGlobal.__jarvisRealtimeConnection
  })
}
