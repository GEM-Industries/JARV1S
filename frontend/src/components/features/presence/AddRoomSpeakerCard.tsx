import React, { useEffect, useRef, useState } from 'react'
import {
  CheckIcon,
  RadioIcon,
  SpinnerIcon,
} from '@phosphor-icons/react'
import { presenceApi, type PresenceNode } from '../../../client/presenceApi'
import { smartHomeApi, type RoomSummary } from '../../../client/smartHomeApi'
import {
  voiceApi,
  type SpeakerProfileStatus,
} from '../../../client/voiceApi'
import { getHostStatus, wsUrlFromHostOrigin } from '../../../runtime/desktopBridge'
import { isDesktopApp } from '../../../runtime/clientSurface'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { Button } from '../../ui/Button'
import { FieldControl, Input } from '../../ui/FieldControl'
import { Select } from '../../ui/Select'
import { connectRoomSpeaker } from './connectSpeaker'
import { RoomSpeakerVoiceSample } from './RoomSpeakerVoiceSample'
import { SetupCard } from './SetupCard'
import { SpeakerPairStatus } from './SpeakerPairStatus'
import { SPEAKER_CONNECT_WAIT_MS, type LanPairStatus } from './pairing'

type IssuedSetup = {
  code: string
  expires_at: string
  command: string
  label: string
}

export const AddRoomSpeakerCard: React.FC = () => {
  const openOverlay = useJarvisStore((s) => s.openOverlay)
  const presenceVersion = useJarvisStore((s) => s.presenceVersion)
  const [rooms, setRooms] = useState<RoomSummary[]>([])
  const [label, setLabel] = useState('Bedroom Speaker')
  const [areaId, setAreaId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [issued, setIssued] = useState<IssuedSetup | null>(null)
  const [lanStatus, setLanStatus] = useState<LanPairStatus>('idle')
  const [connected, setConnected] = useState<PresenceNode | null>(null)
  const [waiting, setWaiting] = useState(false)
  const [privateReady, setPrivateReady] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [voiceProfile, setVoiceProfile] = useState<SpeakerProfileStatus | null>(null)
  const knownOnlineRef = useRef<Set<string>>(new Set())

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
    if (!issued || connected) return
    let active = true
    const checkPresence = async () => {
      try {
        const view = await presenceApi.getPresence()
        if (!active) return
        const node = view.nodes.find(
          (item) =>
            item.kind === 'satellite' &&
            item.status === 'online' &&
            !knownOnlineRef.current.has(item.node_id),
        )
        if (node) {
          setConnected(node)
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
  }, [issued, connected, presenceVersion])

  useEffect(() => {
    if (!issued || connected) return
    setWaiting(true)
    const timeout = window.setTimeout(() => setWaiting(false), SPEAKER_CONNECT_WAIT_MS)
    return () => window.clearTimeout(timeout)
  }, [issued, connected])

  const selectedRoom = rooms.find((room) => room.area_id === areaId) || null

  const selectRoom = (nextAreaId: string) => {
    setAreaId(nextAreaId)
    const room = rooms.find((candidate) => candidate.area_id === nextAreaId)
    if (room) setLabel(`${room.name} Speaker`)
  }

  const issue = async () => {
    setBusy(true)
    setError(null)
    setConnected(null)
    setIssued(null)
    setLanStatus('idle')
    try {
      let wsUrl: string | null = null
      if (isDesktopApp()) {
        const status = await getHostStatus()
        if (status?.remote_healthy !== true || !status.serve_url) {
          throw new Error('Private access is not ready. Finish setup above, then try again.')
        }
        wsUrl = wsUrlFromHostOrigin(status.serve_url)
      } else {
        wsUrl = wsUrlFromHostOrigin(window.location.origin)
      }
      const view = await presenceApi.getPresence().catch(() => null)
      knownOnlineRef.current = new Set(
        (view?.nodes ?? [])
          .filter((node) => node.kind === 'satellite' && node.status === 'online')
          .map((node) => node.node_id),
      )
      const speakerLabel = label.trim() || 'Room Speaker'
      const setup = await connectRoomSpeaker(
        {
          nodeLabel: speakerLabel,
          roomName: selectedRoom?.name || undefined,
          haAreaId: selectedRoom?.area_id || undefined,
          backendUrl: wsUrl,
        },
        (issued) => {
          setIssued({
            code: issued.code,
            expires_at: issued.expiresAt,
            command: issued.command,
            label: speakerLabel,
          })
          setLanStatus(isDesktopApp() ? 'connecting' : 'skipped')
          setExpanded(true)
        },
      )
      setLanStatus(setup.lanStatus)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not create a setup code')
    } finally {
      setBusy(false)
    }
  }

  const reset = () => {
    setIssued(null)
    setConnected(null)
    setWaiting(false)
    setError(null)
    setLanStatus('idle')
  }

  const online = Boolean(connected)

  return (
    <SetupCard
      icon={<RadioIcon size={18} className="shrink-0 text-brand" />}
      title="Room speaker"
      subtitle="Connect a speaker in another room"
      expanded={expanded}
      onToggle={() => setExpanded((value) => !value)}
    >
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
            onClick={() => void issue()}
          >
            {busy ? 'Connecting…' : 'Connect speaker'}
          </Button>
          <p className="type-meta text-foreground-subtle">
            The speaker must already be on and running.
          </p>
        </div>
      )}

      {issued && (
        <div className="flex flex-col gap-4">
          {online && connected ? (
            <div className="flex flex-col gap-3">
              <div className="flex items-center gap-3 overflow-hidden rounded-control bg-status-success/10 px-3 py-3">
                <CheckIcon size={18} className="shrink-0 text-status-success" />
                <div className="min-w-0">
                  <p className="type-label text-status-success">
                    {(connected.node_label || issued.label) + ' is online'}
                  </p>
                  <p className="mt-0.5 type-meta text-foreground-muted">Setup complete</p>
                </div>
              </div>
              <RoomSpeakerVoiceSample
                nodeId={connected.node_id}
                speakerName={connected.node_label || issued.label}
                profile={voiceProfile}
                onCaptured={setVoiceProfile}
              />
            </div>
          ) : (
            <SpeakerPairStatus
              lanStatus={lanStatus}
              waiting={waiting}
              connected={online}
              command={issued.command}
              expiresAt={issued.expires_at}
              onRenew={() => void issue()}
              renewing={busy}
            />
          )}

          <Button
            size="xs"
            variant="ghost"
            color="neutral"
            className="self-start"
            onClick={reset}
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
    </SetupCard>
  )
}
