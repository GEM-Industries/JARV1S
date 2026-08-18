import React, { useEffect, useState } from 'react'
import {
  CaretDownIcon,
  CheckIcon,
  CopyIcon,
  RadioIcon,
  SpinnerIcon,
} from '@phosphor-icons/react'
import {
  createSatelliteCredential,
  type CreateSatelliteCredentialResponse,
} from '../../../client/deviceAuthApi'
import { presenceApi } from '../../../client/presenceApi'
import { smartHomeApi, type RoomSummary } from '../../../client/smartHomeApi'
import { getHostStatus, wsUrlFromHostOrigin } from '../../../runtime/desktopBridge'
import { isDesktopApp } from '../../../runtime/clientSurface'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { Button } from '../../ui/Button'
import { FieldControl, Input } from '../../ui/FieldControl'
import { Select } from '../../ui/Select'

const POLL_TIMEOUT_MS = 120_000

export const AddRoomSpeakerCard: React.FC = () => {
  const openOverlay = useJarvisStore((s) => s.openOverlay)
  const presenceVersion = useJarvisStore((s) => s.presenceVersion)
  const [rooms, setRooms] = useState<RoomSummary[]>([])
  const [label, setLabel] = useState('Bedroom Speaker')
  const [areaId, setAreaId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [issued, setIssued] = useState<CreateSatelliteCredentialResponse | null>(null)
  const [online, setOnline] = useState(false)
  const [waiting, setWaiting] = useState(false)
  const [copied, setCopied] = useState<'url' | 'token' | 'all' | null>(null)
  const [privateReady, setPrivateReady] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    let active = true
    void smartHomeApi
      .getRooms()
      .then((response) => {
        if (active) setRooms(response.rooms.filter((room) => room.exists_in_ha))
      })
      .catch(() => {
        if (active) setRooms([])
      })
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    let active = true
    if (!isDesktopApp()) {
      setPrivateReady(true)
      return
    }
    void getHostStatus()
      .then((status) => {
        if (active) setPrivateReady(status?.remote_healthy === true)
      })
      .catch(() => {
        if (active) setPrivateReady(false)
      })
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    if (!issued || online) return
    let active = true
    const checkPresence = async () => {
      try {
        const view = await presenceApi.getPresence()
        if (!active) return
        const node = view.nodes.find((item) => item.node_id === issued.node_id)
        if (node?.status === 'online') {
          setOnline(true)
          setWaiting(false)
        }
      } catch {
        // The next presence event or fallback interval retries.
      }
    }
    void checkPresence()
    const interval = window.setInterval(() => void checkPresence(), 10_000)
    return () => {
      active = false
      window.clearInterval(interval)
    }
  }, [issued, online, presenceVersion])

  useEffect(() => {
    if (!issued || online) return
    setWaiting(true)
    const timeout = window.setTimeout(() => setWaiting(false), POLL_TIMEOUT_MS)
    return () => window.clearTimeout(timeout)
  }, [issued, online])

  const selectedRoom = rooms.find((room) => room.area_id === areaId) || null

  const selectRoom = (nextAreaId: string) => {
    setAreaId(nextAreaId)
    const room = rooms.find((candidate) => candidate.area_id === nextAreaId)
    if (room) setLabel(`${room.name} Speaker`)
  }

  const mint = async () => {
    setBusy(true)
    setError(null)
    setOnline(false)
    setIssued(null)
    try {
      const result = await createSatelliteCredential({
        node_label: label.trim() || 'Room Speaker',
        ha_area_id: selectedRoom?.area_id || null,
        room_name: selectedRoom?.name || null,
        capabilities: ['mic', 'speaker'],
      })
      let backendWsUrl = result.backend_ws_url
      if (isDesktopApp()) {
        const status = await getHostStatus()
        backendWsUrl = wsUrlFromHostOrigin(status?.serve_url) || backendWsUrl
      }
      setIssued({ ...result, backend_ws_url: backendWsUrl })
      setExpanded(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create room speaker credentials')
    } finally {
      setBusy(false)
    }
  }

  const copyText = async (value: string, key: 'url' | 'token' | 'all') => {
    await navigator.clipboard.writeText(value)
    setCopied(key)
  }

  const setupBlock = issued
    ? [
        `backend_url = "${issued.backend_ws_url}"`,
        `device_token = "${issued.device_token}"`,
        `node_id = "${issued.node_id}"`,
        `node_label = "${issued.node_label || label}"`,
      ].join('\n')
    : ''

  return (
    <section className="overflow-hidden rounded-panel bg-surface/20">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        className="flex min-h-12 w-full items-center gap-3 px-4 py-3 text-left transition-colors duration-feedback hover:bg-surface/30 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70"
      >
        <RadioIcon size={18} className="shrink-0 text-brand" />
        <span className="min-w-0 flex-1">
          <span className="block type-label text-foreground">Room speaker</span>
          <span className="block truncate type-meta text-foreground-subtle">
            Set up a Raspberry Pi satellite
          </span>
        </span>
        <CaretDownIcon
          size={14}
          className={`shrink-0 text-foreground-subtle transition-transform duration-feedback ease-hologram motion-reduce:transition-none ${expanded ? 'rotate-180' : ''}`}
        />
      </button>

      {expanded && (
        <div className="flex flex-col gap-4 border-t border-outline/15 px-4 py-4">
          {privateReady === false && (
            <div className="overflow-hidden rounded-control bg-status-warning/10 px-3 py-3">
              <p className="type-body text-foreground-muted">
                Set up private access above before connecting a room speaker.
              </p>
            </div>
          )}

          {!issued && (
            <div className="flex flex-col gap-4">
              <FieldControl label="Home Assistant room (optional)" htmlFor="speaker-ha-room">
                <Select
                  id="speaker-ha-room"
                  value={areaId}
                  onChange={selectRoom}
                  disabled={busy || privateReady === false}
                  aria-label="Home Assistant room"
                  options={[
                    { value: '', label: 'Assign later' },
                    ...rooms.map((room) => ({ value: room.area_id, label: room.name })),
                  ]}
                />
              </FieldControl>
              {rooms.length === 0 && (
                <div className="flex flex-col gap-2">
                  <p className="type-body text-foreground-muted">
                    No Home Assistant rooms yet. You can assign this speaker later.
                  </p>
                  <Button
                    size="xs"
                    variant="ghost"
                    color="brand"
                    className="self-start"
                    onClick={() => openOverlay('home_assistant')}
                  >
                    Open Home Assistant
                  </Button>
                </div>
              )}
              <FieldControl label="Speaker name" htmlFor="speaker-name">
                <Input
                  id="speaker-name"
                  value={label}
                  maxLength={80}
                  disabled={busy || privateReady === false}
                  onChange={(event) => setLabel(event.target.value)}
                  autoComplete="off"
                />
              </FieldControl>
              <Button
                size="sm"
                color="brand"
                className="self-start"
                disabled={busy || privateReady === false}
                icon={busy ? <SpinnerIcon className="animate-spin" size={14} /> : undefined}
                onClick={() => void mint()}
              >
                {busy ? 'Creating…' : 'Create setup details'}
              </Button>
            </div>
          )}

          {issued && (
            <div className="flex flex-col gap-4 overflow-hidden rounded-control bg-canvas/30 p-4">
              {online ? (
                <div className="flex items-center gap-3 overflow-hidden rounded-control bg-status-success/10 px-3 py-3">
                  <CheckIcon size={18} className="shrink-0 text-status-success" />
                  <div className="min-w-0">
                    <p className="type-label text-status-success">
                      {(issued.node_label || 'Room speaker') + ' is online'}
                    </p>
                    <p className="mt-0.5 type-meta text-foreground-muted">Setup complete</p>
                  </div>
                </div>
              ) : (
                <p className="flex items-center gap-2 type-body text-foreground-muted" role="status">
                  {waiting ? <SpinnerIcon className="animate-spin" size={14} /> : null}
                  {waiting ? 'Waiting for the speaker to connect…' : 'Speaker has not connected yet'}
                </p>
              )}

              {!online && (
                <div className="flex flex-col gap-2">
                  <p className="type-body text-foreground-muted">
                    Paste this into{' '}
                    <span className="font-mono text-foreground">~/.jarvis-satellite/config.toml</span>
                  </p>
                  <pre className="overflow-x-auto rounded-control bg-canvas/40 p-3 font-mono type-meta text-foreground-muted">
                    {setupBlock}
                  </pre>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      size="xs"
                      variant="ghost"
                      color="brand"
                      icon={<CopyIcon size={12} />}
                      onClick={() => void copyText(setupBlock, 'all')}
                    >
                      {copied === 'all' ? 'Copied' : 'Copy config'}
                    </Button>
                    <Button
                      size="xs"
                      variant="ghost"
                      color="neutral"
                      onClick={() => void copyText(issued.backend_ws_url, 'url')}
                    >
                      {copied === 'url' ? 'Copied URL' : 'Copy URL'}
                    </Button>
                    <Button
                      size="xs"
                      variant="ghost"
                      color="neutral"
                      onClick={() => void copyText(issued.device_token, 'token')}
                    >
                      {copied === 'token' ? 'Copied token' : 'Copy token'}
                    </Button>
                  </div>
                  <p className="type-meta text-foreground-subtle">
                    Restart the speaker service. It will appear online automatically.
                  </p>
                </div>
              )}

              <Button
                size="xs"
                variant="ghost"
                color="neutral"
                className="self-start"
                onClick={() => {
                  setIssued(null)
                  setOnline(false)
                  setWaiting(false)
                }}
              >
                {online ? 'Add another speaker' : 'Start over'}
              </Button>
            </div>
          )}

          {error && (
            <p className="type-meta text-status-danger" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </section>
  )
}
