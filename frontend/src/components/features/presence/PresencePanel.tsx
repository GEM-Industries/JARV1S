import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BroadcastIcon,
  CaretLeftIcon,
  CopyIcon,
  DesktopIcon,
  DeviceMobileIcon,
  MapPinIcon,
  PlusIcon,
  PulseIcon,
  RadioIcon,
  SpinnerIcon,
  WarningIcon,
} from '@phosphor-icons/react'
import {
  presenceApi,
  type DeviceKind,
  type PresenceNode,
  type PresenceView,
} from '../../../client/presenceApi'
import { smartHomeApi, type RoomSummary } from '../../../client/smartHomeApi'
import { jarvisClient } from '../../../client/JarvisClient'
import { isDesktopApp } from '../../../runtime/clientSurface'
import {
  checkSpeakerReachability,
  getHostStatus,
  wsUrlFromHostOrigin,
  type HostReachabilityStatus,
  type SpeakerReachability,
} from '../../../runtime/desktopBridge'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { Button } from '../../ui/Button'
import { Placeholder } from '../../ui/Placeholder'
import { Select } from '../../ui/Select'
import { StatusBarWorkspaceHeader } from '../../ui/StatusBarWorkspaceHeader'
import { StatusPill } from '../../ui/StatusPill'
import { HostSettings } from '../settings/HostSettings'
import { AddRoomSpeakerCard } from './AddRoomSpeakerCard'

type PrivateAccess = 'unknown' | 'ready' | 'needs_setup'

type LoadState = 'idle' | 'loading' | 'ready' | 'error'
type FetchMode = 'load' | 'background'

function errMsg(e: unknown, fb: string) {
  return e instanceof Error ? e.message : fb
}

function labelForKind(kind: DeviceKind): string {
  switch (kind) {
    case 'browser':
      return 'Browser'
    case 'desktop':
      return 'Desktop app'
    case 'phone':
      return 'Phone'
    case 'satellite':
      return 'Room speaker'
    default:
      return 'Device'
  }
}

function titleCase(value: string): string {
  return value
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

function displayNameForNode(node: PresenceNode, thisDevice: boolean): string {
  const explicit = node.node_label?.trim()
  if (explicit) return explicit
  if (thisDevice && (node.kind === 'browser' || node.kind === 'desktop')) return 'This Mac'
  if (thisDevice && node.kind === 'phone') return 'This phone'
  if (node.kind === 'browser') return 'Browser'
  if (node.kind === 'desktop') return 'Desktop app'
  if (node.kind === 'phone') return 'Phone'
  if (node.kind === 'satellite') return titleCase(node.node_id)
  return titleCase(node.node_id) || 'Device'
}

const KindIcon: React.FC<{ kind: DeviceKind; size?: number }> = ({ kind, size = 18 }) => {
  switch (kind) {
    case 'browser':
    case 'desktop':
      return <DesktopIcon size={size} className="text-brand shrink-0" />
    case 'phone':
      return <DeviceMobileIcon size={size} className="text-brand shrink-0" />
    case 'satellite':
      return <RadioIcon size={size} className="text-brand shrink-0" />
    default:
      return <BroadcastIcon size={size} className="text-brand shrink-0" />
  }
}

function toneForStatus(status: PresenceNode['status']): 'success' | 'warning' | 'error' | 'neutral' {
  switch (status) {
    case 'online':
      return 'success'
    case 'offline':
      return 'warning'
  }
}

function statusLabel(status: PresenceNode['status']): string {
  switch (status) {
    case 'online':
      return 'Online'
    case 'offline':
      return 'Offline'
  }
}

function labelForCapability(cap: string): string {
  switch (cap) {
    case 'mic':
      return 'Mic'
    case 'speaker':
      return 'Speaker'
    case 'display':
      return 'Display'
    default:
      return titleCase(cap)
  }
}

function formatLastSeen(iso?: string | null): string | null {
  if (!iso) return null
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return null
  const diffMs = Date.now() - date.getTime()
  if (diffMs < 60_000) return 'just now'
  const mins = Math.floor(diffMs / 60_000)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function contextForNode(node: PresenceNode, privateAccess: PrivateAccess): string | null {
  if (node.status === 'online') return null
  const lastSeen = formatLastSeen(node.last_seen_at)
  if (node.kind === 'satellite') {
    if (privateAccess === 'needs_setup') {
      return lastSeen
        ? `Last seen ${lastSeen} · turn on private access on this Mac`
        : 'Offline · turn on private access on this Mac'
    }
    if (privateAccess === 'ready') {
      return lastSeen
        ? `Last seen ${lastSeen} · speaker is not reaching this Mac`
        : 'Offline · speaker is not reaching this Mac'
    }
    return lastSeen
      ? `Last seen ${lastSeen} · check private access and restart the speaker`
      : 'Offline · check private access and restart the speaker'
  }
  return lastSeen ? `Last seen ${lastSeen} · reconnects when reopened` : 'Offline · reconnects when reopened'
}

type SpeakerDiagnosis = { checking: boolean; message: string | null }

function speakerDiagnosisMessage(
  host: HostReachabilityStatus | null,
  speaker: SpeakerReachability | null,
): string {
  if (!host?.backend_healthy) {
    return 'JARV1S on this Mac is not running — restart it, then check again.'
  }
  if (host.remote_healthy !== true) {
    return 'This Mac’s private access is not working — review setup below, then restart the speaker.'
  }
  if (!speaker || speaker.network === 'unavailable') {
    return 'Could not check the speaker — Tailscale is not responding on this Mac.'
  }
  if (speaker.network === 'not_found' || speaker.network === 'offline') {
    const seen = speaker.network === 'offline' ? formatLastSeen(speaker.last_seen) : null
    return `This Mac is fine — the speaker is not on your private network${seen ? ` (last seen ${seen})` : ''}. Check that it is powered on and connected to Wi-Fi. Its address and access are still valid, so no re-pairing is needed.`
  }
  return 'This Mac is fine and the speaker is on the network, but its JARV1S service is not connecting. Restart the speaker; if it stays offline, copy the address below and update its config.'
}

function groupNodes(nodes: PresenceNode[]): { title: string; nodes: PresenceNode[] }[] {
  const online = nodes.filter((n) => n.status === 'online')
  const offline = nodes.filter((n) => n.status === 'offline')
  const groups: { title: string; nodes: PresenceNode[] }[] = []
  if (online.length) groups.push({ title: 'Online', nodes: online })
  if (offline.length) groups.push({ title: 'Offline', nodes: offline })
  return groups
}

function roomOptions(rooms: RoomSummary[]) {
  return [
    { value: '', label: 'No room' },
    ...rooms
      .filter((room) => room.exists_in_ha)
      .map((room) => ({ value: room.area_id, label: room.name })),
  ]
}

const NodeRow: React.FC<{
  node: PresenceNode
  thisDevice: boolean
  rooms: RoomSummary[]
  privateAccess: PrivateAccess
  speakerWsUrl: string | null
  addressCopied: boolean
  revoking: boolean
  confirmId: string | null
  assigning: boolean
  selectedAreaId: string
  assignBusy: boolean
  onSelectedAreaChange: (areaId: string) => void
  onBeginAssign: (node: PresenceNode) => void
  onSubmitAssign: (node: PresenceNode) => void
  onCancelAssign: () => void
  onRevokeClick: (node: PresenceNode) => void
  onConfirmRevoke: (node: PresenceNode) => void
  onCancelRevoke: () => void
  diagnosis?: SpeakerDiagnosis
  onCheckSpeaker?: (node: PresenceNode) => void
  onCopySpeakerAddress?: () => void
  onOpenPrivateAccess?: () => void
  onViewTurns?: (node: PresenceNode) => void
}> = ({
  node,
  thisDevice,
  rooms,
  privateAccess,
  speakerWsUrl,
  addressCopied,
  revoking,
  confirmId,
  assigning,
  selectedAreaId,
  assignBusy,
  onSelectedAreaChange,
  onBeginAssign,
  onSubmitAssign,
  onCancelAssign,
  onRevokeClick,
  onConfirmRevoke,
  onCancelRevoke,
  diagnosis,
  onCheckSpeaker,
  onCopySpeakerAddress,
  onOpenPrivateAccess,
  onViewTurns,
}) => {
  const displayName = displayNameForNode(node, thisDevice)
  const diagnosisMessage =
    node.kind === 'satellite' && node.status === 'offline' ? diagnosis?.message : null
  const context = diagnosisMessage ?? contextForNode(node, privateAccess)
  const canRevoke = !thisDevice && Boolean(node.device_id) && !revoking
  const confirming = Boolean(node.device_id) && confirmId === node.device_id
  const showViewTurns = Boolean(onViewTurns) && node.kind === 'satellite'
  const canAssign = Boolean(node.device_id)
  const showThisDeviceTag = thisDevice
  const offlineSatellite = node.kind === 'satellite' && node.status === 'offline'
  const showCheckSpeaker = offlineSatellite && Boolean(onCheckSpeaker)
  const showCopyAddress =
    offlineSatellite && privateAccess === 'ready' && Boolean(speakerWsUrl) && Boolean(onCopySpeakerAddress)
  const showPrivateAccess =
    offlineSatellite && privateAccess === 'needs_setup' && Boolean(onOpenPrivateAccess)

  const metaParts = [labelForKind(node.kind)]
  if (node.capabilities.length > 0) {
    metaParts.push(node.capabilities.map(labelForCapability).join(' · '))
  }

  if (confirming) {
    return (
      <div className="flex flex-col gap-3 bg-status-danger/10 px-4 py-3">
        <div className="min-w-0">
          <p className="type-label text-foreground">Remove access for {displayName}?</p>
          <p className="mt-1 type-body text-foreground-muted">
            This device will need to pair again to reconnect.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            color="critical"
            size="xs"
            disabled={revoking}
            icon={revoking ? <SpinnerIcon className="animate-spin" size={12} /> : undefined}
            onClick={() => onConfirmRevoke(node)}
          >
            {revoking ? 'Removing…' : 'Remove access'}
          </Button>
          <Button
            variant="ghost"
            color="neutral"
            size="xs"
            disabled={revoking}
            onClick={onCancelRevoke}
          >
            Cancel
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-start gap-3 px-4 py-3">
      <KindIcon kind={node.kind} size={16} />
      <div className="min-w-0 flex-1 flex flex-col gap-2">
        <div className="flex items-center justify-between gap-3">
          <p className="min-w-0 truncate type-label text-foreground">{displayName}</p>
          <StatusPill tone={toneForStatus(node.status)}>{statusLabel(node.status)}</StatusPill>
        </div>

        <div className="min-w-0">
          <p className="truncate type-meta text-foreground-subtle">
            {metaParts.join(' · ')}
            {showThisDeviceTag ? ' · This device' : ''}
          </p>
          {(node.room_name?.trim() || canAssign) && (
            <p className="mt-0.5 truncate type-meta text-foreground-subtle">
              {node.room_name?.trim() || 'No room assigned'}
            </p>
          )}
          {context && <p className="mt-1 type-meta text-foreground-muted">{context}</p>}
          {showCopyAddress && speakerWsUrl && (
            <p className="mt-1 break-all font-mono type-meta text-foreground-subtle">{speakerWsUrl}</p>
          )}
        </div>

        {assigning ? (
          <div className="flex flex-col gap-3 pt-1">
            <Select
              value={selectedAreaId}
              onChange={onSelectedAreaChange}
              options={roomOptions(rooms)}
              aria-label={`Room for ${displayName}`}
            />
            <div className="flex gap-2">
              <Button
                color="brand"
                size="xs"
                disabled={assignBusy}
                icon={assignBusy ? <SpinnerIcon className="animate-spin" size={12} /> : undefined}
                onClick={() => onSubmitAssign(node)}
              >
                {assignBusy ? 'Saving…' : 'Save'}
              </Button>
              <Button
                variant="ghost"
                color="neutral"
                size="xs"
                disabled={assignBusy}
                onClick={onCancelAssign}
              >
                Cancel
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            {canAssign && (
              <Button
                size="xs"
                variant="ghost"
                color="subtle"
                icon={<MapPinIcon size={12} />}
                onClick={() => onBeginAssign(node)}
              >
                {node.ha_area_id ? 'Change room' : 'Assign room'}
              </Button>
            )}
            {showCheckSpeaker && (
              <Button
                size="xs"
                variant="ghost"
                color="action"
                disabled={diagnosis?.checking}
                icon={
                  diagnosis?.checking ? (
                    <SpinnerIcon className="animate-spin" size={12} />
                  ) : (
                    <PulseIcon size={12} />
                  )
                }
                onClick={() => onCheckSpeaker?.(node)}
              >
                {diagnosis?.checking ? 'Checking…' : 'Check speaker'}
              </Button>
            )}
            {showCopyAddress && (
              <Button
                size="xs"
                variant="ghost"
                color="action"
                icon={<CopyIcon size={12} />}
                onClick={onCopySpeakerAddress}
              >
                {addressCopied ? 'Copied address' : 'Copy address'}
              </Button>
            )}
            {showPrivateAccess && (
              <Button size="xs" variant="ghost" color="action" onClick={onOpenPrivateAccess}>
                Review private access
              </Button>
            )}
            {showViewTurns && (
              <Button size="xs" variant="ghost" color="neutral" onClick={() => onViewTurns?.(node)}>
                Activity
              </Button>
            )}
            {canRevoke && (
              <Button size="xs" variant="ghost" color="danger" onClick={() => onRevokeClick(node)}>
                Remove access
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  )
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
  const [assigningNodeId, setAssigningNodeId] = useState<string | null>(null)
  const [selectedAreaId, setSelectedAreaId] = useState('')
  const [assignBusy, setAssignBusy] = useState(false)
  const [privateAccess, setPrivateAccess] = useState<PrivateAccess>(
    isDesktopApp() ? 'unknown' : 'ready',
  )
  const [speakerWsUrl, setSpeakerWsUrl] = useState<string | null>(null)
  const [addressCopied, setAddressCopied] = useState(false)
  const [diagnoses, setDiagnoses] = useState<Record<string, SpeakerDiagnosis>>({})
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
    if (presenceVersion === seenPresenceVersion.current) return
    seenPresenceVersion.current = presenceVersion
    void fetchPresence('background')
  }, [presenceVersion, fetchPresence])

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

  const beginAssign = (node: PresenceNode) => {
    setAssigningNodeId(node.node_id)
    setSelectedAreaId(node.ha_area_id || '')
    setConfirmRevokeId(null)
  }

  const submitAssign = async (node: PresenceNode) => {
    setAssignBusy(true)
    setError(null)
    try {
      setView(await presenceApi.assignNodeRoom(node.node_id, selectedAreaId || null))
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

  const handleCheckSpeaker = useCallback(async (node: PresenceNode) => {
    setDiagnoses((prev) => ({ ...prev, [node.node_id]: { checking: true, message: null } }))
    let message: string
    try {
      const [host, speaker] = await Promise.all([
        getHostStatus(),
        checkSpeakerReachability(node.node_id),
      ])
      message = speakerDiagnosisMessage(host, speaker)
    } catch {
      message = 'Could not run the check — try again.'
    }
    setDiagnoses((prev) => ({ ...prev, [node.node_id]: { checking: false, message } }))
  }, [])

  const handleCopySpeakerAddress = useCallback(async () => {
    if (!speakerWsUrl) return
    await navigator.clipboard.writeText(speakerWsUrl)
    setAddressCopied(true)
    window.setTimeout(() => setAddressCopied(false), 2000)
  }, [speakerWsUrl])

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
                    <NodeRow
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
                      onSubmitAssign={(n) => void submitAssign(n)}
                      onCancelAssign={() => setAssigningNodeId(null)}
                      onRevokeClick={handleRevokeClick}
                      onConfirmRevoke={handleConfirmRevoke}
                      onCancelRevoke={() => setConfirmRevokeId(null)}
                      diagnosis={diagnoses[node.node_id]}
                      onCheckSpeaker={
                        isDesktopApp() ? (n) => void handleCheckSpeaker(n) : undefined
                      }
                      onCopySpeakerAddress={() => void handleCopySpeakerAddress()}
                      onOpenPrivateAccess={handleOpenPrivateAccess}
                      onViewTurns={handleViewTurns}
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
