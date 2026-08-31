import React from 'react'
import {
  BroadcastIcon,
  CopyIcon,
  DesktopIcon,
  DeviceMobileIcon,
  MapPinIcon,
  PulseIcon,
  RadioIcon,
  SpinnerIcon,
} from '@phosphor-icons/react'
import type { PresenceNode } from '../../../client/presenceApi'
import type { RoomSummary } from '../../../client/smartHomeApi'
import type { SpeakerProfileStatus } from '../../../client/voiceApi'
import { ActionMenu } from '../../ui/ActionMenu'
import { Button } from '../../ui/Button'
import { FieldControl } from '../../ui/FieldControl'
import { Select } from '../../ui/Select'
import { StatusPill } from '../../ui/StatusPill'
import { TextLink } from '../../ui/TextLink'
import { RoomSpeakerVoiceSample } from './RoomSpeakerVoiceSample'
import { SpeakerPairStatus } from './SpeakerPairStatus'
import {
  contextForNode,
  displayNameForNode,
  labelForKind,
  statusLabel,
  toneForStatus,
  type PrivateAccess,
  type SpeakerDiagnosis,
  type SpeakerReconnect,
} from './deviceDisplay'

function roomOptions(rooms: RoomSummary[]) {
  return [
    { value: '', label: 'No room' },
    ...rooms
      .filter((room) => room.exists_in_ha)
      .map((room) => ({ value: room.area_id, label: room.name })),
  ]
}

const KindIcon: React.FC<{ kind: PresenceNode['kind']; size?: number }> = ({ kind, size = 18 }) => {
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

export const DeviceRow: React.FC<{
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
  onSubmitAssign: (node: PresenceNode, areaId: string) => void
  onCancelAssign: () => void
  onRevokeClick: (node: PresenceNode) => void
  onConfirmRevoke: (node: PresenceNode) => void
  onCancelRevoke: () => void
  holdBusy: boolean
  onDisconnect: (node: PresenceNode) => void
  onResume: (node: PresenceNode) => void
  diagnosis?: SpeakerDiagnosis
  onCheckSpeaker?: (node: PresenceNode) => void
  onCopySpeakerAddress?: () => void
  onOpenPrivateAccess?: () => void
  onViewTurns?: (node: PresenceNode) => void
  voiceProfile?: SpeakerProfileStatus | null
  onVoiceSampleCaptured?: (status: SpeakerProfileStatus) => void
  reconnect?: SpeakerReconnect | null
  reconnectBusy?: boolean
  reconnectWaiting?: boolean
  onReconnect?: (node: PresenceNode) => void
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
  holdBusy,
  onDisconnect,
  onResume,
  diagnosis,
  onCheckSpeaker,
  onCopySpeakerAddress,
  onOpenPrivateAccess,
  onViewTurns,
  voiceProfile,
  onVoiceSampleCaptured,
  reconnect,
  reconnectBusy,
  reconnectWaiting = false,
  onReconnect,
}) => {
  const displayName = displayNameForNode(node, thisDevice)
  const confirming = Boolean(node.device_id) && confirmId === node.device_id
  const canRevoke = !thisDevice && Boolean(node.device_id) && !revoking
  const canDisconnect = canRevoke && node.status === 'online'
  const canResume = node.disconnected && Boolean(node.device_id)
  const canAssign = Boolean(node.device_id)
  const offlineSatellite = node.kind === 'satellite' && node.status === 'offline' && !node.disconnected
  const needsPrivateAccess = offlineSatellite && privateAccess === 'needs_setup'
  const canReconnect = offlineSatellite && Boolean(onReconnect)
  const canCopyAddress =
    offlineSatellite && privateAccess === 'ready' && Boolean(speakerWsUrl) && Boolean(onCopySpeakerAddress)
  const canViewTurns = Boolean(onViewTurns) && node.kind === 'satellite'

  const roomLabel = node.room_name?.trim() || 'Assign room'
  const diagnosisMessage = offlineSatellite ? diagnosis?.message : null
  const context = diagnosisMessage ?? contextForNode(node, privateAccess)

  const showReconnectPanel = Boolean(reconnect) && !assigning
  const primary =
    assigning || confirming || showReconnectPanel
      ? null
      : canResume
        ? {
            label: holdBusy ? 'Resuming…' : 'Resume',
            onClick: () => onResume(node),
            busy: holdBusy,
          }
        : needsPrivateAccess && onOpenPrivateAccess
        ? { label: 'Review private access', onClick: onOpenPrivateAccess }
        : canReconnect
          ? {
              label: reconnectBusy ? 'Connecting…' : 'Reconnect',
              onClick: () => onReconnect?.(node),
              busy: reconnectBusy,
            }
          : null

  const overflow: React.ReactNode[] = []
  if (canAssign) {
    overflow.push(
      <ActionMenu.Item
        key="room"
        icon={<MapPinIcon size={14} />}
        onClick={() => onBeginAssign(node)}
      >
        {node.room_name?.trim() ? 'Change room' : 'Assign room'}
      </ActionMenu.Item>,
    )
  }
  if (needsPrivateAccess && canReconnect) {
    overflow.push(
      <ActionMenu.Item key="reconnect" disabled={reconnectBusy} onClick={() => onReconnect?.(node)}>
        Reconnect
      </ActionMenu.Item>,
    )
  }
  if (offlineSatellite && onCheckSpeaker) {
    overflow.push(
      <ActionMenu.Item
        key="check"
        icon={<PulseIcon size={14} />}
        disabled={diagnosis?.checking}
        onClick={() => onCheckSpeaker(node)}
      >
        {diagnosis?.checking ? 'Checking…' : diagnosisMessage ? 'Check again' : 'Check speaker'}
      </ActionMenu.Item>,
    )
  }
  if (canCopyAddress) {
    overflow.push(
      <ActionMenu.Item key="copy" icon={<CopyIcon size={14} />} onClick={onCopySpeakerAddress}>
        {addressCopied ? 'Copied address' : 'Copy address'}
      </ActionMenu.Item>,
    )
  }
  if (canViewTurns) {
    overflow.push(
      <ActionMenu.Item key="activity" onClick={() => onViewTurns?.(node)}>
        Activity
      </ActionMenu.Item>,
    )
  }
  if (canDisconnect) {
    overflow.push(
      <ActionMenu.Item key="disconnect" disabled={holdBusy} onClick={() => onDisconnect(node)}>
        Disconnect
      </ActionMenu.Item>,
    )
  }
  const hasOverflow = overflow.length > 0 || canRevoke

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
    <div className="flex flex-col gap-2 px-4 py-4">
      <div className="flex items-center gap-3">
        <KindIcon kind={node.kind} size={16} />
        <p className="min-w-0 flex-1 truncate type-label text-foreground">{displayName}</p>
        <div className="flex shrink-0 items-center gap-1">
          <StatusPill tone={toneForStatus(node)}>{statusLabel(node)}</StatusPill>
          {hasOverflow && !assigning && (
            <ActionMenu label={`More actions for ${displayName}`}>
              {overflow}
              {canRevoke && overflow.length > 0 && <ActionMenu.Separator />}
              {canRevoke && (
                <ActionMenu.Item tone="danger" onClick={() => onRevokeClick(node)}>
                  Remove access
                </ActionMenu.Item>
              )}
            </ActionMenu>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-3 pl-7">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="type-meta text-foreground-subtle">
            <span>
              {labelForKind(node.kind)}
              {thisDevice ? ' · This device' : ''}
            </span>
            {canAssign && !assigning && (
              <>
                <span> · </span>
                <TextLink className="type-meta" onClick={() => onBeginAssign(node)}>
                  {roomLabel}
                </TextLink>
              </>
            )}
            {!canAssign && node.room_name?.trim() && (
              <span> · {node.room_name.trim()}</span>
            )}
          </div>
          {context && <p className="type-meta text-foreground-subtle">{context}</p>}
        </div>

        {assigning && (
          <div className="flex flex-col gap-2">
            <FieldControl label="Room" htmlFor={`room-${node.node_id}`}>
              <Select
                id={`room-${node.node_id}`}
                value={selectedAreaId}
                onChange={(areaId) => {
                  onSelectedAreaChange(areaId)
                  onSubmitAssign(node, areaId)
                }}
                options={roomOptions(rooms)}
                disabled={assignBusy}
                aria-label={`Room for ${displayName}`}
              />
            </FieldControl>
            <TextLink className="self-start type-meta" onClick={onCancelAssign}>
              Cancel
            </TextLink>
          </div>
        )}

        {node.kind === 'satellite' && node.status === 'online' && (
          <RoomSpeakerVoiceSample
            nodeId={node.node_id}
            speakerName={displayName}
            profile={voiceProfile ?? null}
            onCaptured={onVoiceSampleCaptured}
          />
        )}
        {showReconnectPanel && reconnect && (
          <SpeakerPairStatus
            lanStatus={reconnect.lanStatus}
            waiting={reconnectWaiting}
            connected={false}
            command={reconnect.command}
            expiresAt={reconnect.expiresAt}
            onRenew={onReconnect ? () => onReconnect(node) : undefined}
            renewing={reconnectBusy}
          />
        )}

        {primary && (
          <Button
            size="xs"
            color="brand"
            className="self-start"
            disabled={primary.busy}
            icon={primary.busy ? <SpinnerIcon className="animate-spin" size={12} /> : undefined}
            onClick={primary.onClick}
          >
            {primary.label}
          </Button>
        )}
      </div>
    </div>
  )
}
