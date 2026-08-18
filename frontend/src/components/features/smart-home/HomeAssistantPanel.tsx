import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ArrowSquareOutIcon,
  CaretLeftIcon,
  SpinnerIcon,
  WarningIcon,
} from '@phosphor-icons/react'
import {
  smartHomeApi,
  type RoomSummary,
  type RoomsResponse,
  type SmartHomeStatusResponse,
  type SmartHomeUiStatus,
} from '../../../client/smartHomeApi'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { cn } from '../../../utils/cn'
import { Button } from '../../ui/Button'
import { PanelSection } from '../../ui/PanelSection'
import { Placeholder } from '../../ui/Placeholder'
import { StatusBarWorkspaceHeader } from '../../ui/StatusBarWorkspaceHeader'
import { StatusDot, type StatusDotStatus } from '../../ui/StatusDot'
import { StatusPill } from '../../ui/StatusPill'
import { useFadeThrough } from '../../ui/useFadeThrough'
import { HomeAssistantSetup } from './HomeAssistantSetup'
import { homeAssistantHost, openHomeAssistant } from './homeAssistantUrl'
import { RoomsManager } from './RoomsManager'

type LoadState = 'idle' | 'loading' | 'ready' | 'error'
type PanelMode = 'overview' | 'rooms'

function errMsg(e: unknown, fb: string) {
  return e instanceof Error ? e.message : fb
}

function labelForStatus(status: SmartHomeUiStatus): string {
  switch (status) {
    case 'ready':
      return 'Connected'
    case 'unconfigured':
      return 'Not connected'
    case 'invalid_config':
      return 'Invalid configuration'
    case 'unreachable':
      return 'Home Assistant offline'
    case 'auth_failed':
      return 'Authorization expired'
    case 'registry_unavailable':
      return 'Starting up'
    case 'empty_inventory':
      return 'No devices yet'
  }
}

type StatusSurfaceTone = 'success' | 'warning' | 'danger' | 'neutral'

function statusSurfaceTone(
  status: SmartHomeUiStatus,
  { allDevicesOffline = false }: { allDevicesOffline?: boolean } = {},
): StatusSurfaceTone {
  if (allDevicesOffline) return 'warning'
  switch (status) {
    case 'ready':
      return 'success'
    case 'auth_failed':
      return 'danger'
    case 'invalid_config':
    case 'unreachable':
    case 'registry_unavailable':
      return 'warning'
    default:
      return 'neutral'
  }
}

function statusDotForTone(tone: StatusSurfaceTone): StatusDotStatus {
  if (tone === 'danger') return 'error'
  if (tone === 'success') return 'success'
  if (tone === 'warning') return 'warning'
  return 'neutral'
}

const statusSurfaceClass: Record<StatusSurfaceTone, string> = {
  success: 'border border-status-success/35 bg-status-success/10',
  warning: 'border border-status-warning/35 bg-status-warning/10',
  danger: 'border border-status-danger/35 bg-status-danger/10',
  neutral: 'bg-surface/20',
}

const statusLabelClass: Record<StatusSurfaceTone, string> = {
  success: 'text-status-success',
  warning: 'text-status-warning',
  danger: 'text-status-danger-fg',
  neutral: 'text-foreground',
}

const ConnectionStatus: React.FC<{
  title: string
  tone: StatusSurfaceTone
  host?: string | null
  message: string
  nextAction?: string | null
}> = ({ title, tone, host, message, nextAction }) => (
  <div className={cn('flex flex-col gap-1 overflow-hidden rounded-control px-4 py-3', statusSurfaceClass[tone])}>
    <div className="flex items-center gap-2 min-w-0">
      <StatusDot status={statusDotForTone(tone)} size="md" />
      <p className={cn('min-w-0 truncate type-label', statusLabelClass[tone])}>{title}</p>
      {host && (
        <span className="ml-auto shrink-0 type-meta text-foreground-subtle">{host}</span>
      )}
    </div>
    <p className="type-body text-foreground-muted">{message}</p>
    {nextAction && <p className="type-body text-foreground-muted">{nextAction}</p>}
  </div>
)

function needsSetupFlow(status: SmartHomeUiStatus): boolean {
  return (
    status === 'unconfigured' ||
    status === 'invalid_config' ||
    status === 'auth_failed'
  )
}

function deviceTone(state: string): 'success' | 'warning' | 'neutral' {
  const normalized = state.trim().toLowerCase()
  if (normalized === 'on' || normalized === 'open') return 'success'
  if (normalized === 'unavailable' || normalized === 'unknown') return 'warning'
  return 'neutral'
}

function formatDeviceState(state: string): string {
  const normalized = state.trim().toLowerCase()
  if (normalized === 'unavailable') return 'Offline'
  if (normalized === 'unknown') return 'Unknown'
  return state
}

function formatCapability(cap: string): string {
  return cap === 'on_off' ? 'on/off' : cap.replace(/_/g, ' ')
}

type Device = SmartHomeStatusResponse['devices'][number]

function groupByRoom(devices: Device[]): [string, Device[]][] {
  const groups = new Map<string, Device[]>()
  for (const d of devices) {
    const room = d.area_name?.trim() || 'Unassigned'
    const list = groups.get(room)
    if (list) list.push(d)
    else groups.set(room, [d])
  }
  return [...groups.entries()]
}

const DeviceRow: React.FC<{ device: Device }> = ({ device }) => {
  const tone = deviceTone(device.state)
  return (
    <div className="flex items-center justify-between gap-3 px-4 py-3">
      <div className="min-w-0">
        <p className="truncate type-label text-foreground">{device.name}</p>
        {device.capabilities.length > 0 && (
          <p className="mt-0.5 truncate type-meta text-foreground-subtle">
            {device.capabilities.map(formatCapability).join(' · ')}
          </p>
        )}
      </div>
      <StatusPill tone={tone}>{formatDeviceState(device.state)}</StatusPill>
    </div>
  )
}

const OverviewLinkRow: React.FC<{
  title: string
  subtitle: string
  error?: string | null
  onManage: () => void
}> = ({ title, subtitle, error, onManage }) => (
  <div className="flex items-center justify-between gap-3 px-4 py-3">
    <div className="min-w-0">
      <p className="truncate type-label text-foreground">{title}</p>
      <p className="mt-0.5 truncate type-meta text-foreground-subtle">{subtitle}</p>
      {error && (
        <p className="mt-1 type-meta text-status-danger" role="alert">
          {error}
        </p>
      )}
    </div>
    <Button variant="ghost" color="brand" size="xs" className="shrink-0" onClick={onManage}>
      Manage
    </Button>
  </div>
)

export const HomeAssistantPanelContent: React.FC = () => {
  const closeOverlay = useJarvisStore((s) => s.closeOverlay)
  const openOverlay = useJarvisStore((s) => s.openOverlay)

  const [loadState, setLoadState] = useState<LoadState>('idle')
  const [panelMode, setPanelMode] = useState<PanelMode>('overview')
  const [navDirection, setNavDirection] = useState<1 | -1>(1)
  const { rendered: viewMode, className: viewTransitionClass } = useFadeThrough(
    panelMode,
    navDirection,
  )
  const [status, setStatus] = useState<SmartHomeStatusResponse | null>(null)
  const [roomsResponse, setRoomsResponse] = useState<RoomsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [roomsError, setRoomsError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [roomAction, setRoomAction] = useState<string | null>(null)
  const [newRoomName, setNewRoomName] = useState('')
  const [isAddingRoom, setIsAddingRoom] = useState(false)
  const [editingAreaId, setEditingAreaId] = useState<string | null>(null)
  const [editRoomName, setEditRoomName] = useState('')
  const [deleteRoomConfirm, setDeleteRoomConfirm] = useState<RoomSummary | null>(null)
  const [confirmingDisconnect, setConfirmingDisconnect] = useState(false)
  const [disconnecting, setDisconnecting] = useState(false)
  const [setupError, setSetupError] = useState<string | null>(null)

  const fetchStatus = useCallback(async ({ silent = false }: { silent?: boolean } = {}) => {
    if (silent) {
      setRefreshing(true)
    } else {
      setLoadState('loading')
    }
    setError(null)
    setRoomsError(null)
    try {
      const nextStatus = await smartHomeApi.getStatus()
      setStatus(nextStatus)
      const canLoadRooms = nextStatus.configured && nextStatus.authenticated && nextStatus.registry_access
      if (canLoadRooms) {
        try {
          setRoomsResponse(await smartHomeApi.getRooms())
        } catch (roomsErr) {
          setRoomsResponse(null)
          setRoomsError(errMsg(roomsErr, 'Could not load rooms.'))
        }
      } else {
        setRoomsResponse(null)
      }
      setLoadState('ready')
    } catch (e) {
      setError(errMsg(e, 'Could not load Home Assistant status.'))
      if (!silent) setLoadState('error')
    } finally {
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    void fetchStatus()
  }, [fetchStatus])

  const showSetup = status ? needsSetupFlow(status.status) : false
  const offlineDeviceCount = useMemo(
    () =>
      (status?.devices ?? []).filter((device) => {
        const state = device.state.trim().toLowerCase()
        return state === 'unavailable' || state === 'unknown'
      }).length,
    [status?.devices],
  )
  const allDevicesOffline =
    (status?.devices.length ?? 0) > 0 && offlineDeviceCount === (status?.devices.length ?? 0)

  const handleDisconnect = useCallback(async () => {
    setDisconnecting(true)
    setSetupError(null)
    try {
      const nextStatus = await smartHomeApi.disconnect()
      setStatus(nextStatus)
      setRoomsResponse(null)
      setConfirmingDisconnect(false)
      void fetchStatus({ silent: true })
    } catch (e) {
      setSetupError(errMsg(e, 'Could not remove Home Assistant connection.'))
    } finally {
      setDisconnecting(false)
    }
  }, [fetchStatus])

  const host = homeAssistantHost(status?.ha_url)
  const rooms = useMemo(() => groupByRoom(status?.devices ?? []), [status?.devices])
  const managedRooms = roomsResponse?.rooms ?? []
  const speakerCount = managedRooms.reduce((count, room) => count + room.bound_nodes.length, 0)

  const handleCreateRoom = useCallback(async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const name = newRoomName.trim()
    if (!name) return
    setRoomAction('create')
    setRoomsError(null)
    try {
      const result = await smartHomeApi.createRoom(name)
      setRoomsResponse({ rooms: result.rooms })
      setNewRoomName('')
      setIsAddingRoom(false)
      void fetchStatus({ silent: true })
    } catch (e) {
      setRoomsError(errMsg(e, 'Could not create room.'))
    } finally {
      setRoomAction(null)
    }
  }, [fetchStatus, newRoomName])

  const beginRename = useCallback((room: RoomSummary) => {
    setEditingAreaId(room.area_id)
    setEditRoomName(room.name)
    setDeleteRoomConfirm(null)
    setRoomsError(null)
  }, [])

  const cancelRename = useCallback(() => {
    setEditingAreaId(null)
    setEditRoomName('')
  }, [])

  const submitRename = useCallback(async (areaId: string) => {
    const name = editRoomName.trim()
    if (!name) return
    setRoomAction(`rename:${areaId}`)
    setRoomsError(null)
    try {
      const result = await smartHomeApi.renameRoom(areaId, name)
      setRoomsResponse({ rooms: result.rooms })
      cancelRename()
      void fetchStatus({ silent: true })
    } catch (e) {
      setRoomsError(errMsg(e, 'Could not rename room.'))
    } finally {
      setRoomAction(null)
    }
  }, [cancelRename, editRoomName, fetchStatus])

  const handleDeleteRoom = useCallback(async (room: RoomSummary) => {
    setRoomAction(`delete:${room.area_id}`)
    setRoomsError(null)
    try {
      const result = await smartHomeApi.deleteRoom(room.area_id)
      setRoomsResponse({ rooms: result.rooms })
      setDeleteRoomConfirm(null)
      void fetchStatus({ silent: true })
    } catch (e) {
      setRoomsError(errMsg(e, 'Could not delete room.'))
    } finally {
      setRoomAction(null)
    }
  }, [fetchStatus])

  return (
    <div className={cn('flex h-full min-h-0 flex-col', viewTransitionClass)}>
      <StatusBarWorkspaceHeader
        title={viewMode === 'rooms' ? 'Rooms' : 'Home Assistant'}
        titleId="smart-home-title"
        onClose={closeOverlay}
        closeLabel="Close Home Assistant"
        leading={
          <Button
            variant="ghost"
            color="action"
            size="icon-sm"
            onClick={() => {
              if (viewMode === 'overview') {
                openOverlay('smart_home')
              } else {
                setNavDirection(-1)
                setPanelMode('overview')
                setIsAddingRoom(false)
                setEditingAreaId(null)
                setDeleteRoomConfirm(null)
              }
            }}
            aria-label={viewMode === 'overview' ? 'Back to Home' : 'Back to Home Assistant'}
            icon={<CaretLeftIcon size={12} weight="bold" />}
          />
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin">
        <div className="flex w-full max-w-2xl flex-col gap-4 px-6 pb-6 pt-4">
          {loadState === 'loading' && !status && (
            <Placeholder>Checking Home Assistant…</Placeholder>
          )}

          {loadState === 'error' && !status && (
            <div className="space-y-4">
              <div className="flex items-start gap-3">
                <WarningIcon size={16} className="text-status-danger/70 flex-shrink-0 mt-0.5" />
                <p className="type-body text-foreground-muted">{error}</p>
              </div>
              <Button
                variant="ghost"
                color="brand"
                size="xs"
                className="self-start"
                onClick={() => void fetchStatus()}
              >
                Retry
              </Button>
            </div>
          )}

          {status && (
            <>
              {showSetup && viewMode === 'overview' ? (
                <div className="flex flex-col gap-4">
                  {(status.status === 'auth_failed' || status.status === 'invalid_config') && (
                    <ConnectionStatus
                      title={labelForStatus(status.status)}
                      tone={statusSurfaceTone(status.status)}
                      host={host}
                      message={status.message}
                      nextAction={status.next_action}
                    />
                  )}
                  <HomeAssistantSetup
                    initialStatus={status}
                    onConnected={(next) => {
                      setStatus(next)
                      setSetupError(null)
                      void fetchStatus({ silent: true })
                    }}
                  />
                </div>
              ) : null}

              {!showSetup && viewMode === 'overview' && (
                <div className="flex flex-col gap-6">
                  <ConnectionStatus
                    title={
                      allDevicesOffline
                        ? 'Devices offline'
                        : status.status === 'empty_inventory'
                          ? 'Connected'
                          : labelForStatus(status.status)
                    }
                    tone={statusSurfaceTone(status.status, { allDevicesOffline })}
                    host={host}
                    message={
                      allDevicesOffline
                        ? 'Connected to Home Assistant, but every listed device is offline.'
                        : status.status === 'empty_inventory'
                          ? 'Home Assistant is connected. Add your first device there, then refresh.'
                          : status.message
                    }
                    nextAction={
                      allDevicesOffline
                        ? 'Fix power or network in Home Assistant, then Refresh.'
                        : status.next_action
                    }
                  />

                  {status.devices.length > 0 && (
                    <div className="flex flex-col gap-6">
                      {rooms.map(([room, devices]) => (
                        <div key={room} className="flex flex-col gap-2">
                          <div className="flex items-baseline justify-between gap-3 px-4">
                            <p className="type-label-small text-foreground-muted">{room}</p>
                            <span className="type-meta tabular-nums text-foreground-subtle">
                              {devices.length}
                            </span>
                          </div>
                          <div className="ui-surface-group">
                            {devices.map((device) => (
                              <DeviceRow key={device.entity_id} device={device} />
                            ))}
                          </div>
                        </div>
                      ))}
                      {status.devices_truncated && (
                        <p className="type-meta text-foreground-subtle text-center">
                          Showing first {status.devices.length} of your controllable devices
                        </p>
                      )}
                    </div>
                  )}

                  <div className="ui-surface-group">
                    {roomsResponse && (
                      <OverviewLinkRow
                        title="Rooms"
                        subtitle={
                          `${managedRooms.length} room${managedRooms.length === 1 ? '' : 's'}` +
                          (speakerCount > 0
                            ? ` · ${speakerCount} speaker${speakerCount === 1 ? '' : 's'} bound`
                            : '')
                        }
                        error={roomsError}
                        onManage={() => {
                          setNavDirection(1)
                          setPanelMode('rooms')
                        }}
                      />
                    )}
                    <OverviewLinkRow
                      title="JARV1S devices"
                      subtitle="This Mac, phones, and room speakers"
                      onManage={() => openOverlay('presence')}
                    />
                  </div>

                  <div
                    className={cn(
                      'grid gap-2',
                      status.ha_url ? 'grid-cols-[minmax(0,2fr)_minmax(104px,1fr)]' : 'grid-cols-1',
                    )}
                  >
                    {status.ha_url && (
                      <Button
                        className="w-full"
                        color="brand"
                        size="md"
                        icon={<ArrowSquareOutIcon size={16} />}
                        onClick={() => openHomeAssistant(status.ha_url!)}
                      >
                        Open Home Assistant
                      </Button>
                    )}
                    <Button
                      color="subtle"
                      size="md"
                      className="w-full"
                      disabled={refreshing}
                      icon={refreshing ? <SpinnerIcon className="animate-spin" size={16} /> : undefined}
                      onClick={() => void fetchStatus({ silent: true })}
                    >
                      {refreshing ? 'Refreshing' : 'Refresh'}
                    </Button>
                  </div>
                </div>
              )}

              {status.configured && viewMode === 'overview' && (
                <PanelSection
                  as="section"
                  className="flex w-full flex-col gap-4"
                  aria-labelledby="home-assistant-connection-title"
                >
                  <div className="flex items-center justify-between gap-4">
                    <div className="min-w-0">
                      <h3
                        id="home-assistant-connection-title"
                        className="type-label text-foreground"
                      >
                        Connection
                      </h3>
                      <p className="mt-1 truncate type-meta text-foreground-subtle">
                        {host ? `Home Assistant at ${host}` : 'Home Assistant is connected'}
                      </p>
                    </div>
                    {!confirmingDisconnect && (
                      <Button
                        variant="ghost"
                        color="danger"
                        size="sm"
                        shape="control"
                        className="shrink-0"
                        disabled={disconnecting}
                        onClick={() => {
                          setSetupError(null)
                          setConfirmingDisconnect(true)
                        }}
                      >
                        Disconnect
                      </Button>
                    )}
                  </div>

                  {confirmingDisconnect && (
                    <div className="flex flex-col gap-3" aria-live="polite">
                      <p className="type-body text-foreground-muted">
                        Disconnect JARV1S from Home Assistant? Your devices, rooms, and automations
                        will stay unchanged in Home Assistant.
                      </p>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          variant="ghost"
                          color="neutral"
                          size="sm"
                          shape="control"
                          disabled={disconnecting}
                          onClick={() => setConfirmingDisconnect(false)}
                        >
                          Cancel
                        </Button>
                        <Button
                          color="critical"
                          size="sm"
                          shape="control"
                          disabled={disconnecting}
                          icon={
                            disconnecting
                              ? <SpinnerIcon className="animate-spin" size={14} />
                              : undefined
                          }
                          onClick={() => void handleDisconnect()}
                        >
                          {disconnecting ? 'Disconnecting…' : 'Confirm disconnect'}
                        </Button>
                      </div>
                    </div>
                  )}

                  {setupError && (
                    <p className="type-body text-status-danger-fg" role="alert">
                      {setupError}
                    </p>
                  )}
                </PanelSection>
              )}

              {roomsResponse && viewMode === 'rooms' && (
                <RoomsManager
                  rooms={managedRooms}
                  roomsError={roomsError}
                  roomAction={roomAction}
                  newRoomName={newRoomName}
                  isAddingRoom={isAddingRoom}
                  editingAreaId={editingAreaId}
                  editRoomName={editRoomName}
                  deleteRoomConfirm={deleteRoomConfirm}
                  onNewRoomNameChange={setNewRoomName}
                  onStartAdd={() => {
                    setIsAddingRoom(true)
                    setEditingAreaId(null)
                    setDeleteRoomConfirm(null)
                  }}
                  onCancelAdd={() => {
                    setIsAddingRoom(false)
                    setNewRoomName('')
                  }}
                  onCreateRoom={handleCreateRoom}
                  onBeginRename={beginRename}
                  onEditRoomNameChange={setEditRoomName}
                  onSubmitRename={(areaId) => void submitRename(areaId)}
                  onCancelRename={cancelRename}
                  onRequestDelete={(room) => {
                    setDeleteRoomConfirm(room)
                    setEditingAreaId(null)
                    setRoomsError(null)
                  }}
                  onConfirmDelete={(room) => void handleDeleteRoom(room)}
                  onCancelDelete={() => setDeleteRoomConfirm(null)}
                />
              )}

              {error && status && (
                <p className="type-body text-status-danger-fg text-center" role="alert">
                  {error}
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
