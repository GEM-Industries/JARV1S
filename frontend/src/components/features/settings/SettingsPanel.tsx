import React, { useEffect, useState } from 'react'
import {
  ArrowLeftIcon,
  CaretRightIcon,
  DesktopTowerIcon,
  KeyIcon,
  MicrophoneIcon,
  SlidersHorizontalIcon,
} from '@phosphor-icons/react'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { isDesktopApp } from '../../../runtime/clientSurface'
import { cn } from '../../../utils/cn'
import { CredentialsPanel } from '../integrations/CredentialsPanel'
import { StatusBarWorkspaceHeader } from '../../ui/StatusBarWorkspaceHeader'
import { AudioSettings } from './AudioSettings'
import { HostSettings } from './HostSettings'

type SettingsSection = 'audio' | 'model' | 'credentials' | 'host'

const sections: Array<{
  id: SettingsSection
  label: string
  description: string
  icon: React.ReactNode
}> = [
  {
    id: 'audio',
    label: 'Voice & Audio',
    description: 'Microphone, transcription, and spoken replies',
    icon: <MicrophoneIcon size={17} />,
  },
  {
    id: 'model',
    label: 'AI model',
    description: 'Choose response speed, quality, and privacy',
    icon: <SlidersHorizontalIcon size={17} />,
  },
  {
    id: 'credentials',
    label: 'Connections',
    description: 'Optional upgrades for search, apps, and delegated work',
    icon: <KeyIcon size={17} />,
  },
  {
    id: 'host',
    label: 'This Mac',
    description: 'Launch and background behavior',
    icon: <DesktopTowerIcon size={17} />,
  },
]

export const SettingsPanelContent: React.FC = () => {
  const closeOverlay = useJarvisStore((state) => state.closeOverlay)
  const initialSection = useJarvisStore((state) => state.settingsInitialSection)
  const desktop = isDesktopApp()
  const visibleSections = desktop ? sections : sections.filter((item) => item.id !== 'host')
  const initialVisibleSection = initialSection === 'host' && !desktop ? 'audio' : initialSection
  const [section, setSection] = useState<SettingsSection>(initialVisibleSection ?? 'audio')
  const [mobileDetailOpen, setMobileDetailOpen] = useState(Boolean(initialVisibleSection))
  const activeSection = visibleSections.find((item) => item.id === section)

  useEffect(() => {
    if (initialSection && (desktop || initialSection !== 'host')) {
      setSection(initialSection)
      setMobileDetailOpen(true)
    }
  }, [desktop, initialSection])

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <StatusBarWorkspaceHeader
        title="Settings"
        titleId="settings-title"
        subtitle="Configure this JARV1S host"
        onClose={closeOverlay}
        closeLabel="Close settings"
      />

      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <aside className="hidden shrink-0 border-r border-outline/15 md:block md:w-60">
          <nav
            aria-label="Settings sections"
            className="space-y-1 py-3"
          >
            {visibleSections.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-current={section === item.id ? 'page' : undefined}
                onClick={() => setSection(item.id)}
                className={cn(
                  'group mx-2 flex min-h-12 w-[calc(100%-1rem)] items-center gap-3 rounded-control px-3 text-left transition-colors duration-feedback',
                  'ui-surface-selectable focus:outline-none',
                  section === item.id
                    ? 'ui-surface-selected text-foreground'
                    : 'text-foreground-muted hover:text-foreground',
                )}
              >
                <span className={cn(section === item.id ? 'text-brand' : 'text-foreground-subtle')}>
                  {item.icon}
                </span>
                <span className="min-w-0">
                  <span className="block type-label">{item.label}</span>
                </span>
              </button>
            ))}
          </nav>
        </aside>

        {!mobileDetailOpen && (
          <nav aria-label="Settings sections" className="space-y-1 px-4 py-3 md:hidden">
            {visibleSections.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  setSection(item.id)
                  setMobileDetailOpen(true)
                }}
                className="flex min-h-14 w-full items-center gap-3 rounded-control px-3 text-left text-foreground-muted transition-colors duration-feedback hover:bg-surface/15 hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70"
              >
                <span className="text-foreground-subtle">{item.icon}</span>
                <span className="min-w-0 flex-1">
                  <span className="block type-label text-foreground">{item.label}</span>
                  <span className="mt-0.5 block type-meta text-foreground-subtle">{item.description}</span>
                </span>
                <CaretRightIcon size={16} className="shrink-0 text-foreground-subtle" aria-hidden />
              </button>
            ))}
          </nav>
        )}

        <div className={cn('min-h-0 min-w-0 flex-1 flex-col', mobileDetailOpen ? 'flex' : 'hidden md:flex')}>
          <div className="shrink-0 px-6 pb-4 pt-3">
            <button
              type="button"
              onClick={() => setMobileDetailOpen(false)}
              className="-ml-2 mb-3 flex min-h-10 items-center gap-2 rounded-control px-2 type-label text-foreground-muted transition-colors duration-feedback hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/70 md:hidden"
            >
              <ArrowLeftIcon size={16} aria-hidden />
              Settings
            </button>
            <p className="type-section text-foreground">
              {activeSection?.label}
            </p>
            <p className="mt-1 type-body text-foreground-muted">
              {activeSection?.description}.
            </p>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin">
            {section === 'audio' && <AudioSettings />}
            {section === 'model' && (
              <CredentialsPanel key="model" active section="model" />
            )}
            {section === 'credentials' && (
              <>
                <CredentialsPanel key="credentials" active section="credentials" />
                <HostSettings section="updates" />
              </>
            )}
            {section === 'host' && <HostSettings section="startup" />}
          </div>
        </div>
      </div>
    </div>
  )
}
