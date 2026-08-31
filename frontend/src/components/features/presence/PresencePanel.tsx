import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CaretLeftIcon,
  PlusIcon,
  WarningIcon,
} from '@phosphor-icons/react'
import {
  presenceApi,
  type PresenceNode,
  type PresenceView,
} from '../../../client/presenceApi'
import { smartHomeApi, type RoomSummary } from '../../../client/smartHomeApi'
import { jarvisClient } from '../../../client/JarvisClient'
import { voiceApi, type SpeakerProfileStatus } from '../../../client/voiceApi'
import { isDesktopApp } from '../../../runtime/clientSurface'
import {
  checkSpeakerReachability,
  getHostStatus,
  wsUrlFromHostOrigin,
} from '../../../runtime/desktopBridge'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { Button } from '../../ui/Button'
import { Placeholder } from '../../ui/Placeholder'
import { StatusBarWorkspaceHeader } from '../../ui/StatusBarWorkspaceHeader'
import { HostSettings } from '../settings/HostSettings'
import { AddRoomSpeakerCard } from './AddRoomSpeakerCard'
import { connectRoomSpeaker } from './connectSpeaker'
import { DeviceRow } from './DeviceRow'
import {
  displayNameForNode,
  groupNodes,
  speakerDiagnosisMessage,
  type PrivateAccess,
  type SpeakerDiagnosis,
  type SpeakerReconnect,
} from './deviceDisplay'
import { SPEAKER_CONNECT_WAIT_MS } from './pairing'

type LoadState = 'idle' | 'loading' | 'ready' | 'error'
type FetchMode = 'load' | 'background'

function errMsg(e: unknown, fb: string) {
  return e instanceof Error ? e.message : fb
}

export const PresencePanelContent: React.FC = () => {
  const closeOverlay = useJarvisStore((s) => s.closeOverlay)
  const openOverlay = useJarvisStore((s) => s.openOverlay)
  const presenceVersion = useJarvisStore((s) => s.presenceVersion)
  const seenPresenceVersion = useRef(presenceVersion)

  const [loadState, setLoadState] = useState<LoadState>('idle')
  const [view, setView] = useState<PresenceView | null>(null)
  const [rooms, setRooms] = useState<RoomSummary[]>([])
  const [error, setError] = useState<string | null>(null)
  const [revokingId, setRevokingId] = useState<string | null>(null)
  const [confirmRevokeId, setConfirmRevokeId] = useState<string | null>(null)
  const [holdBusyId, setHoldBusyId] = useState<string | null>(null)
  const [assigningNodeId, setAssigningNodeId] = useState<string | null>(null)
  const [selectedAreaId, setSelectedAreaId] = useState('')
  const [assignBusy, setAssignBusy] = useState(false)
  const [privateAccess, setPrivateAccess] = useState<PrivateAccess>(
    isDesktopApp() ? 'unknown' : 'ready',
  )
  const [speakerWsUrl, setSpeakerWsUrl] = useState<string | null>(null)
  const [addressCopied, setAddressCopied] = useState(false)
  const [diagnoses, setDiagnoses] = useState<Record<string, SpeakerDiagnosis>>({})
  const [voiceProfile, setVoiceProfile] = useState<SpeakerProfileStatus | null>(null)
  const [reconnect, setReconnect] = useState<(SpeakerReconnect & { nodeId: string }) | null>(null)
  const [reconnectBusyId, setReconnectBusyId] = useState<string | null>(null)
  const [reconnectWaiting, setReconnectWaiting] = useState(false)
  const accessSetupRef = useRef<HTMLDivElement>(null)

  const thisNodeId = useMemo(() => jarvisClient.getNodeId(), [])

  const refreshPrivateAccess = useCallback(async () => {
    if (!isDesktopApp()) {
      setPrivateAccess('ready')
      setSpeakerWsUrl(null)
      return
    }
    try {
      const status = await getHostStatus()
      const ready = status?.remote_healthy === true
      setPrivateAccess(ready ? 'ready' : 'needs_setup')
      setSpeakerWsUrl(wsUrlFromHostOrigin(status?.serve_url))
    } catch {
      setPrivateAccess('unknown')
      setSpeakerWsUrl(null)
    }
  }, [])

  const fetchPresence = useCallback(async (mode: FetchMode = 'load') => {
    if (mode === 'load') setLoadState('loading')
    if (mode !== 'background') setError(null)
    try {
      const [nextView, roomsResponse] = await Promise.all([
        presenceApi.getPresence(),
        smartHomeApi.getRooms().catch(() => ({ rooms: [] as RoomSummary[] })),
        refreshPrivateAccess(),
      ])
      setView(nextView)
      setRooms(roomsResponse.rooms)
      setLoadState('ready')
    } catch (e) {
      if (mode !== 'background') {
        setError(errMsg(e, 'Could not load devices.'))
      }
      if (mode === 'load') setLoadState('error')
    }
  }, [refreshPrivateAccess])

  useEffect(() => {
    void fetchPresence()
  }, [fetchPresence])

  useEffect(() => {
    let active = true
    void voiceApi
      .getSpeakerProfile()
      .then((status) => {
        if (active) setVoiceProfile(status)
      })
      .catch(() => {
        if (active) setVoiceProfile(null)
      })
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    if (presenceVersion === seenPresenceVersion.current) return
    seenPresenceVersion.current = presenceVersion
    void fetchPresence('background')
  }, [presenceVersion, fetchPresence])

  useEffect(() => {
    if (!reconnect || !view) return
    const node = view.nodes.find((item) => item.node_id === reconnect.nodeId)
    if (node?.status === 'online') {
      setReconnect(null)
    }
  }, [view, reconnect])

  useEffect(() => {
    if (!reconnect) {
      setReconnectWaiting(false)
      return
    }
    setReconnectWaiting(true)
    const timeout = window.setTimeout(() => setReconnectWaiting(false), SPEAKER_CONNECT_WAIT_MS)
    return () => window.clearTimeout(timeout)
  }, [reconnect?.nodeId, reconnect?.expiresAt])

  const handleCheckSpeaker = useCallback(async (node: PresenceNode) => {
    setDiagnoses((prev) => ({ ...prev, [node.node_id]: { checking: true, message: null } }))
    try {
      const [host, speaker] = await Promise.all([
        getHostStatus(),
        checkSpeakerReachability(node.node_id),
      ])
      setDiagnoses((prev) => ({
        ...prev,
        [node.node_id]: { checking: false, message: speakerDiagnosisMessage(host, speaker) },
      }))
    } catch {
      setDiagnoses((prev) => ({ ...prev, [node.node_id]: { checking: false, message: null } }))
    }
  }, [])

  const groups = useMemo(() => groupNodes(view?.nodes ?? []), [view?.nodes])
  const onlineCount = view?.nodes.filter((n) => n.status === 'online').length ?? 0
  const offlineCount = view?.nodes.filter((n) => n.status === 'offline').length ?? 0

  const handleRevokeClick = (node: PresenceNode) => {
    if (!node.device_id) return
    setConfirmRevokeId(node.device_id)
    setAssigningNodeId(null)
  }

  const handleConfirmRevoke = async (node: PresenceNode) => {
    if (!node.device_id) return
    setRevokingId(node.device_id)
    setError(null)
    try {
      await presenceApi.revokeDevice(node.device_id)
      setConfirmRevokeId(null)
      await fetchPresence('background')
    } catch (e) {
      setError(errMsg(e, 'Could not revoke device.'))
    } finally {
      setRevokingId(null)
    }
  }

  const handleDisconnect = async (node: PresenceNode) => {
    if (!node.device_id) return
    setHoldBusyId(node.device_id)
    setError(null)
    try {
      await presenceApi.disconnectDevice(node.device_id)
      await fetchPresence('background')
    } catch (e) {
      setError(errMsg(e, 'Could not disconnect device.'))
    } finally {
      setHoldBusyId(null)
    }
  }

  const handleResume = async (node: PresenceNode) => {
    if (!node.device_id) return
    setHoldBusyId(node.device_id)
    setError(null)
    try {
      await presenceApi.resumeDevice(node.device_id)
      await fetchPresence('background')
    } catch (e) {
      setError(errMsg(e, 'Could not resume device.'))
    } finally {
      setHoldBusyId(null)
    }
  }

  const beginAssign = (node: PresenceNode) => {
    setAssigningNodeId(node.node_id)
    setSelectedAreaId(node.ha_area_id || '')
    setConfirmRevokeId(null)
  }

  const submitAssign = async (node: PresenceNode, areaId: string) => {
    setAssignBusy(true)
    setError(null)
    try {
      setView(await presenceApi.assignNodeRoom(node.node_id, areaId || null))
      setAssigningNodeId(null)
    } catch (e) {
      setError(errMsg(e, 'Could not assign room.'))
    } finally {
      setAssignBusy(false)
    }
  }

  const handleViewTurns = useCallback((node: PresenceNode) => {
    openOverlay('operations', {
      runKind: 'user',
      nodeId: node.node_id,
      nodeLabel: displayNameForNode(node, node.node_id === thisNodeId),
    })
  }, [openOverlay, thisNodeId])

  const handleCopySpeakerAddress = useCallback(async () => {
    if (!speakerWsUrl) return
    await navigator.clipboard.writeText(speakerWsUrl)
    setAddressCopied(true)
    window.setTimeout(() => setAddressCopied(false), 2000)
  }, [speakerWsUrl])

  const handleReconnect = useCallback(async (node: PresenceNode) => {
    setReconnectBusyId(node.node_id)
    if (reconnect?.nodeId !== node.node_id) setReconnect(null)
    setError(null)
    try {
      const setup = await connectRoomSpeaker(
        {
          nodeLabel: displayNameForNode(node, false),
          nodeId: node.node_id,
          roomName: node.room_name || undefined,
          haAreaId: node.ha_area_id || undefined,
          backendUrl: speakerWsUrl,
        },
        (issued) => {
          setReconnect({
            nodeId: node.node_id,
            command: issued.command,
            expiresAt: issued.expiresAt,
            lanStatus: isDesktopApp() ? 'connecting' : 'skipped',
          })
        },
      )
      setReconnect((current) =>
        current?.nodeId === node.node_id
          ? { ...current, command: setup.command, expiresAt: setup.expiresAt, lanStatus: setup.lanStatus }
          : current,
      )
    } catch (e) {
      setError(errMsg(e, 'Could not reconnect the speaker.'))
    } finally {
      setReconnectBusyId(null)
    }
  }, [reconnect, speakerWsUrl])

  const handleOpenPrivateAccess = useCallback(() => {
    accessSetupRef.current?.scrollIntoView({ block: 'start' })
  }, [])

  const statusSubtitle = view
    ? `${onlineCount} online${offlineCount ? ` · ${offlineCount} offline` : ''}`
    : 'Phones, room speakers, and this Mac'

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <StatusBarWorkspaceHeader
        title="Devices"
        titleId="presence-title"
        subtitle={statusSubtitle}
        onClose={closeOverlay}
        closeLabel="Close Devices"
        leading={
          <Button
            variant="ghost"
            color="action"
            size="icon-sm"
            onClick={() => openOverlay('smart_home')}
            aria-label="Back to Home"
            icon={<CaretLeftIcon size={12} weight="bold" />}
          />
        }
      />

      <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-6 pb-6 pt-4 scrollbar-thin">
        {loadState === 'loading' && !view && (
          <Placeholder>Loading devices…</Placeholder>
        )}

        {loadState === 'error' && !view && (
          <div className="flex flex-col gap-4">
            <div className="flex items-start gap-3">
              <WarningIcon size={16} className="mt-0.5 flex-shrink-0 text-status-danger/70" />
              <p className="type-body text-foreground-muted">{error}</p>
            </div>
            <Button
              variant="ghost"
              color="brand"
              size="xs"
              className="self-start"
              onClick={() => void fetchPresence()}
            >
              Retry
            </Button>
          </div>
        )}

        {view && (
          <>
            {groups.length === 0 && (
              <Placeholder tone="muted">No devices yet. Add your first device below.</Placeholder>
            )}

            {groups.map((group) => (
              <div key={group.title} className="flex flex-col gap-2">
                <div className="flex items-baseline justify-between gap-3 px-4">
                  <p className="type-label-small text-foreground-muted">{group.title}</p>
                  <span className="type-meta tabular-nums text-foreground-subtle">
                    {group.nodes.length}
                  </span>
                </div>
                <div className="ui-surface-group">
                  {group.nodes.map((node) => (
                    <DeviceRow
                      key={node.node_id}
                      node={node}
                      thisDevice={node.node_id === thisNodeId}
                      rooms={rooms}
                      privateAccess={privateAccess}
                      speakerWsUrl={speakerWsUrl}
                      addressCopied={addressCopied}
                      revoking={Boolean(node.device_id) && revokingId === node.device_id}
                      confirmId={confirmRevokeId}
                      assigning={assigningNodeId === node.node_id}
                      selectedAreaId={selectedAreaId}
                      assignBusy={assignBusy}
                      onSelectedAreaChange={setSelectedAreaId}
                      onBeginAssign={beginAssign}
                      onSubmitAssign={(n, areaId) => void submitAssign(n, areaId)}
                      onCancelAssign={() => setAssigningNodeId(null)}
                      onRevokeClick={handleRevokeClick}
                      onConfirmRevoke={handleConfirmRevoke}
                      onCancelRevoke={() => setConfirmRevokeId(null)}
                      holdBusy={Boolean(node.device_id) && holdBusyId === node.device_id}
                      onDisconnect={(n) => void handleDisconnect(n)}
                      onResume={(n) => void handleResume(n)}
                      diagnosis={diagnoses[node.node_id]}
                      onCheckSpeaker={
                        isDesktopApp() ? (n) => void handleCheckSpeaker(n) : undefined
                      }
                      onCopySpeakerAddress={() => void handleCopySpeakerAddress()}
                      onOpenPrivateAccess={handleOpenPrivateAccess}
                      onViewTurns={handleViewTurns}
                      voiceProfile={voiceProfile}
                      onVoiceSampleCaptured={setVoiceProfile}
                      reconnect={reconnect?.nodeId === node.node_id ? reconnect : null}
                      reconnectBusy={reconnectBusyId === node.node_id}
                      reconnectWaiting={reconnect?.nodeId === node.node_id && reconnectWaiting}
                      onReconnect={(n) => void handleReconnect(n)}
                    />
                  ))}
                </div>
              </div>
            ))}

            <div className="flex flex-col gap-3">
              <div className="flex items-center gap-2 px-4">
                <PlusIcon size={14} className="shrink-0 text-brand" />
                <p className="type-label-small text-foreground-muted">Add a device</p>
              </div>
              <div className="flex flex-col gap-3">
                <div ref={accessSetupRef} className="scroll-mt-4">
                  <HostSettings
                    section="access"
                    embedded
                    onAccessChange={() => void refreshPrivateAccess()}
                  />
                </div>
                <AddRoomSpeakerCard />
              </div>
            </div>

            {error && view && (
              <p className="type-meta text-center text-status-danger" role="alert">
                {error}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}
