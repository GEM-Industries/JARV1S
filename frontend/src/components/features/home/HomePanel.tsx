import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  ArrowsClockwiseIcon,
  CaretRightIcon,
  DeviceMobileIcon,
  HouseIcon,
} from '@phosphor-icons/react'
import { presenceApi, type PresenceView } from '../../../client/presenceApi'
import {
  smartHomeApi,
  type SmartHomeStatusResponse,
} from '../../../client/smartHomeApi'
import {
  getHostStatus,
  type HostReachabilityStatus,
} from '../../../runtime/desktopBridge'
import { isDesktopApp } from '../../../runtime/clientSurface'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { Button } from '../../ui/Button'
import { Placeholder } from '../../ui/Placeholder'
import { StatusBarWorkspaceHeader } from '../../ui/StatusBarWorkspaceHeader'
import { StatusPill } from '../../ui/StatusPill'

type LoadState = 'loading' | 'ready' | 'error'

interface HomeDestinationProps {
  icon: React.ReactNode
  title: string
  description: string
  status: string
  tone: 'success' | 'warning' | 'neutral'
  onClick: () => void
}

const HomeDestination: React.FC<HomeDestinationProps> = ({
  icon,
  title,
  description,
  status,
  tone,
  onClick,
}) => (
  <button
    type="button"
    className="ui-surface-selectable flex min-h-20 w-full items-center gap-3 px-4 py-3 text-left transition-colors duration-feedback focus:outline-none"
    onClick={onClick}
  >
    <span className="shrink-0 text-brand" aria-hidden>
      {icon}
    </span>
    <span className="min-w-0 flex-1">
      <span className="flex flex-wrap items-center justify-between gap-2">
        <span className="type-label text-foreground">{title}</span>
        <StatusPill tone={tone}>{status}</StatusPill>
      </span>
      <span className="mt-1 block type-meta text-foreground-subtle">{description}</span>
    </span>
    <CaretRightIcon size={16} className="shrink-0 text-foreground-subtle" aria-hidden />
  </button>
)

export const HomePanelContent: React.FC = () => {
  const closeOverlay = useJarvisStore((state) => state.closeOverlay)
  const openOverlay = useJarvisStore((state) => state.openOverlay)
  const presenceVersion = useJarvisStore((state) => state.presenceVersion)
  const [presence, setPresence] = useState<PresenceView | null>(null)
  const [host, setHost] = useState<HostReachabilityStatus | null>(null)
  const [homeAssistant, setHomeAssistant] = useState<SmartHomeStatusResponse | null>(null)
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [refreshing, setRefreshing] = useState(false)
  const hasLoaded = useRef(false)

  const refresh = useCallback(async (background = false) => {
    if (background) setRefreshing(true)
    else setLoadState('loading')

    const [presenceResult, homeAssistantResult, hostResult] = await Promise.allSettled([
      presenceApi.getPresence(),
      smartHomeApi.getStatus(),
      isDesktopApp() ? getHostStatus() : Promise.resolve(null),
    ])

    if (presenceResult.status === 'fulfilled') setPresence(presenceResult.value)
    if (homeAssistantResult.status === 'fulfilled') setHomeAssistant(homeAssistantResult.value)
    if (hostResult.status === 'fulfilled') setHost(hostResult.value)

    const hasResult =
      presenceResult.status === 'fulfilled' ||
      homeAssistantResult.status === 'fulfilled' ||
      hostResult.status === 'fulfilled'
    if (hasResult) setLoadState('ready')
    else if (!background) setLoadState('error')
    hasLoaded.current = true
    setRefreshing(false)
  }, [])

  useEffect(() => {
    void refresh(hasLoaded.current)
  }, [presenceVersion, refresh])

  const onlineCount = presence?.nodes.filter((node) => node.status === 'online').length ?? 0
  const deviceCount = presence?.nodes.length ?? 0
  const offlineCount = Math.max(0, deviceCount - onlineCount)
  const privateAccessNeedsSetup = isDesktopApp() && host?.remote_healthy === false
  const devicesStatus = privateAccessNeedsSetup
    ? 'Setup needed'
    : presence
      ? deviceCount > 0
        ? `${onlineCount} of ${deviceCount} online`
        : 'No devices'
      : 'Unavailable'
  const devicesDescription = privateAccessNeedsSetup
    ? 'Finish private access before adding phones or room speakers'
    : offlineCount > 0
      ? `${offlineCount} device${offlineCount === 1 ? '' : 's'} needs to reconnect`
      : deviceCount > 0
        ? 'Manage this Mac, phones, room speakers, and private access'
        : 'Connect your first phone or room speaker'

  const homeAssistantStatus = homeAssistant?.ready
    ? 'Connected'
    : homeAssistant?.configured
      ? 'Needs attention'
      : 'Optional'
  const homeAssistantDescription = homeAssistant?.ready
    ? `${homeAssistant.device_count} device${homeAssistant.device_count === 1 ? '' : 's'} across ${homeAssistant.area_count} room${homeAssistant.area_count === 1 ? '' : 's'}`
    : homeAssistant?.configured
      ? homeAssistant.message
      : 'Set up or connect Home Assistant to control rooms and devices'

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <StatusBarWorkspaceHeader
        title="Home"
        titleId="home-title"
        subtitle="Manage JARV1S devices and Home Assistant"
        onClose={closeOverlay}
        closeLabel="Close Home"
      />

      <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-6 pt-4 scrollbar-thin">
        <div className="flex w-full max-w-2xl flex-col gap-6">
          {loadState === 'loading' && !presence && !homeAssistant && (
            <Placeholder>Checking your home…</Placeholder>
          )}

          {loadState === 'error' && (
            <div className="flex items-center justify-between gap-3">
              <Placeholder tone="muted">Home status is unavailable.</Placeholder>
              <Button size="xs" variant="ghost" color="brand" onClick={() => void refresh()}>
                Retry
              </Button>
            </div>
          )}

          {loadState === 'ready' && (
            <>
              <div className="ui-surface-group">
                <HomeDestination
                  icon={<DeviceMobileIcon size={20} />}
                  title="Devices"
                  description={devicesDescription}
                  status={devicesStatus}
                  tone={
                    privateAccessNeedsSetup || offlineCount > 0
                      ? 'warning'
                      : presence && deviceCount > 0
                        ? 'success'
                        : 'neutral'
                  }
                  onClick={() => openOverlay('presence')}
                />
                <HomeDestination
                  icon={<HouseIcon size={20} />}
                  title="Home Assistant"
                  description={homeAssistantDescription}
                  status={homeAssistantStatus}
                  tone={homeAssistant?.ready ? 'success' : homeAssistant?.configured ? 'warning' : 'neutral'}
                  onClick={() => openOverlay('home_assistant')}
                />
              </div>

              <Button
                className="self-start"
                size="sm"
                variant="ghost"
                color="subtle"
                icon={<ArrowsClockwiseIcon size={15} />}
                disabled={refreshing}
                onClick={() => void refresh(true)}
              >
                {refreshing ? 'Refreshing…' : 'Refresh status'}
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
