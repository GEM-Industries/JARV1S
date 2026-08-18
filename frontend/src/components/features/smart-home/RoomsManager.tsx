import React from 'react'
import {
  PencilSimpleIcon,
  PlusIcon,
  SpinnerIcon,
  TrashIcon,
} from '@phosphor-icons/react'
import type { RoomSummary } from '../../../client/smartHomeApi'
import { cn } from '../../../utils/cn'
import { Button } from '../../ui/Button'
import { FieldControl, Input } from '../../ui/FieldControl'

function roomMeta(room: RoomSummary): string {
  const parts = [`${room.device_count} device${room.device_count === 1 ? '' : 's'}`]
  if (room.bound_nodes.length > 0) {
    parts.push(`${room.bound_nodes.length} speaker${room.bound_nodes.length === 1 ? '' : 's'}`)
  }
  if (!room.exists_in_ha) {
    parts.push('missing in HA')
  }
  return parts.join(' · ')
}

function boundNodeLabel(room: RoomSummary): string {
  return room.bound_nodes.map((node) => node.node_label || node.node_id).join(', ')
}

export interface RoomsManagerProps {
  rooms: RoomSummary[]
  roomsError: string | null
  roomAction: string | null
  newRoomName: string
  isAddingRoom: boolean
  editingAreaId: string | null
  editRoomName: string
  deleteRoomConfirm: RoomSummary | null
  onNewRoomNameChange: (value: string) => void
  onStartAdd: () => void
  onCancelAdd: () => void
  onCreateRoom: (event: React.FormEvent<HTMLFormElement>) => void
  onBeginRename: (room: RoomSummary) => void
  onEditRoomNameChange: (value: string) => void
  onSubmitRename: (areaId: string) => void
  onCancelRename: () => void
  onRequestDelete: (room: RoomSummary) => void
  onConfirmDelete: (room: RoomSummary) => void
  onCancelDelete: () => void
}

export const RoomsManager: React.FC<RoomsManagerProps> = ({
  rooms,
  roomsError,
  roomAction,
  newRoomName,
  isAddingRoom,
  editingAreaId,
  editRoomName,
  deleteRoomConfirm,
  onNewRoomNameChange,
  onStartAdd,
  onCancelAdd,
  onCreateRoom,
  onBeginRename,
  onEditRoomNameChange,
  onSubmitRename,
  onCancelRename,
  onRequestDelete,
  onConfirmDelete,
  onCancelDelete,
}) => {
  const rowActionsLocked =
    Boolean(roomAction) || isAddingRoom || editingAreaId != null || deleteRoomConfirm != null

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between gap-3">
        <p className="min-w-0 type-body text-foreground-muted">
          Create, rename, or remove rooms in Home Assistant.
        </p>
        {!isAddingRoom && (
          <Button
            variant="ghost"
            color="brand"
            size="xs"
            className="shrink-0"
            icon={<PlusIcon size={12} />}
            onClick={onStartAdd}
          >
            Add
          </Button>
        )}
      </div>

      {isAddingRoom && (
        <form className="overflow-hidden rounded-control bg-surface/20 px-4 py-3 flex flex-col gap-3" onSubmit={onCreateRoom}>
          <FieldControl label="Room name" htmlFor="ha-room-name">
            <Input
              id="ha-room-name"
              value={newRoomName}
              onChange={(event) => onNewRoomNameChange(event.target.value)}
              placeholder="Kitchen"
              autoComplete="off"
              autoFocus
            />
          </FieldControl>
          <div className="flex gap-2">
            <Button
              color="brand"
              size="xs"
              type="submit"
              disabled={roomAction === 'create' || !newRoomName.trim()}
              icon={
                roomAction === 'create' ? <SpinnerIcon className="animate-spin" size={12} /> : undefined
              }
            >
              {roomAction === 'create' ? 'Saving…' : 'Save'}
            </Button>
            <Button
              variant="ghost"
              color="neutral"
              size="xs"
              disabled={roomAction === 'create'}
              onClick={onCancelAdd}
            >
              Cancel
            </Button>
          </div>
        </form>
      )}

      {roomsError && (
        <p className="type-meta text-status-danger" role="alert">
          {roomsError}
        </p>
      )}

      {rooms.length === 0 && !isAddingRoom ? (
        <p className="type-body text-foreground-muted">No rooms yet. Add one to get started.</p>
      ) : rooms.length > 0 ? (
        <div className="ui-surface-group">
          {rooms.map((room) => {
            const isEditing = editingAreaId === room.area_id
            const isDeleting = deleteRoomConfirm?.area_id === room.area_id

            if (isDeleting && deleteRoomConfirm) {
              return (
                <div
                  key={room.area_id}
                  className="flex flex-col gap-3 bg-status-danger/10 px-4 py-3"
                >
                  <div className="min-w-0">
                    <p className="type-label text-foreground">Delete {deleteRoomConfirm.name}?</p>
                    <p className="mt-1 type-body text-foreground-muted">
                      {deleteRoomConfirm.device_count > 0 || deleteRoomConfirm.entity_count > 0
                        ? 'Home Assistant may unassign devices from this room.'
                        : 'This removes the room from Home Assistant.'}
                    </p>
                    {deleteRoomConfirm.bound_nodes.length > 0 && (
                      <p className="mt-1 type-meta text-foreground-muted">
                        Speakers stay online: {boundNodeLabel(deleteRoomConfirm)}
                      </p>
                    )}
                  </div>
                  <div className="flex gap-2">
                    <Button
                      color="critical"
                      size="xs"
                      disabled={Boolean(roomAction)}
                      icon={
                        roomAction === `delete:${deleteRoomConfirm.area_id}` ? (
                          <SpinnerIcon className="animate-spin" size={12} />
                        ) : (
                          <TrashIcon size={12} />
                        )
                      }
                      onClick={() => onConfirmDelete(deleteRoomConfirm)}
                    >
                      Delete
                    </Button>
                    <Button
                      variant="ghost"
                      color="neutral"
                      size="xs"
                      disabled={Boolean(roomAction)}
                      onClick={onCancelDelete}
                    >
                      Cancel
                    </Button>
                  </div>
                </div>
              )
            }

            if (isEditing) {
              return (
                <form
                  key={room.area_id}
                  className="flex flex-col gap-3 px-4 py-3"
                  onSubmit={(event) => {
                    event.preventDefault()
                    onSubmitRename(room.area_id)
                  }}
                >
                  <FieldControl label="Room name" htmlFor={`ha-rename-${room.area_id}`}>
                    <Input
                      id={`ha-rename-${room.area_id}`}
                      value={editRoomName}
                      onChange={(event) => onEditRoomNameChange(event.target.value)}
                      autoComplete="off"
                      autoFocus
                    />
                  </FieldControl>
                  <div className="flex gap-2">
                    <Button
                      color="brand"
                      size="xs"
                      type="submit"
                      disabled={Boolean(roomAction) || !editRoomName.trim()}
                      icon={
                        roomAction === `rename:${room.area_id}` ? (
                          <SpinnerIcon className="animate-spin" size={12} />
                        ) : undefined
                      }
                    >
                      {roomAction === `rename:${room.area_id}` ? 'Saving…' : 'Save'}
                    </Button>
                    <Button
                      variant="ghost"
                      color="neutral"
                      size="xs"
                      type="button"
                      disabled={Boolean(roomAction)}
                      onClick={onCancelRename}
                    >
                      Cancel
                    </Button>
                  </div>
                </form>
              )
            }

            return (
              <div
                key={room.area_id}
                className={cn(
                  'flex items-center justify-between gap-3 px-4 py-3',
                  !room.exists_in_ha && 'bg-status-warning/10',
                )}
              >
                <div className="min-w-0">
                  <p className="truncate type-label text-foreground">{room.name}</p>
                  <p className="mt-0.5 truncate type-meta text-foreground-subtle">
                    {roomMeta(room)}
                    {room.bound_nodes.length > 0 ? ` · ${boundNodeLabel(room)}` : ''}
                  </p>
                </div>
                <div className="flex shrink-0 gap-1">
                  <Button
                    variant="ghost"
                    color="subtle"
                    size="icon-sm"
                    disabled={rowActionsLocked || !room.exists_in_ha}
                    icon={<PencilSimpleIcon size={12} />}
                    onClick={() => onBeginRename(room)}
                    aria-label={`Rename ${room.name}`}
                  />
                  <Button
                    variant="ghost"
                    color="danger"
                    size="icon-sm"
                    disabled={rowActionsLocked || !room.exists_in_ha}
                    icon={<TrashIcon size={12} />}
                    onClick={() => onRequestDelete(room)}
                    aria-label={`Delete ${room.name}`}
                  />
                </div>
              </div>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}
