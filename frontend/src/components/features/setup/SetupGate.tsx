import React, { useCallback, useEffect, useState } from 'react'
import { SpinnerIcon } from '@phosphor-icons/react'
import { setupApi } from '../../../client/setupApi'
import { jarvisClient } from '../../../client/JarvisClient'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { Button } from '../../ui/Button'
import { Hologram } from '../../ui/Hologram'
import { SetupWizard } from './SetupWizard'

interface SetupGateProps {
  children: React.ReactNode
}

interface SetupStatusProps {
  title: string
  detail: string
  loading?: boolean
  action?: React.ReactNode
  error?: boolean
}

const SetupStatus: React.FC<SetupStatusProps> = ({
  title,
  detail,
  loading = false,
  action,
  error = false,
}) => (
  <main className="stage-background h-dvh overflow-y-auto">
    <div className="flex min-h-full items-center justify-center p-6">
      <Hologram
        variant="base"
        color={error ? 'error' : 'default'}
        className="w-full max-w-md bg-canvas-sunken/90 p-8 text-center shadow-2xl shadow-black/20"
      >
        <div
          className="flex flex-col items-center gap-4"
          role={error ? 'alert' : loading ? 'status' : undefined}
        >
          {loading && <SpinnerIcon className="animate-spin text-brand" size={28} aria-hidden />}
          <div className="space-y-2">
            <h1 className="type-section text-foreground">{title}</h1>
            <p className="type-body text-foreground-muted">{detail}</p>
          </div>
          {action}
        </div>
      </Hologram>
    </div>
  </main>
)

export const SetupGate: React.FC<SetupGateProps> = ({ children }) => {
  const setupState = useJarvisStore((s) => s.setupState)
  const setupLoading = useJarvisStore((s) => s.setupLoading)
  const setupRequired = useJarvisStore((s) => s.setupRequired)
  const setSetupState = useJarvisStore((s) => s.setSetupState)
  const setSetupLoading = useJarvisStore((s) => s.setSetupLoading)
  const setSetupRequired = useJarvisStore((s) => s.setSetupRequired)

  const [bootError, setBootError] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)

  const refreshSetupState = useCallback(async () => {
    setSetupLoading(true)
    setBootError(null)
    try {
      const state = await setupApi.getState()
      setSetupState(state)
      setSetupRequired(!state.core_ready)
      return state
    } catch (e) {
      setBootError(e instanceof Error ? e.message : 'Could not reach JARV1S')
      return null
    } finally {
      setSetupLoading(false)
    }
  }, [setSetupLoading, setSetupRequired, setSetupState])

  useEffect(() => {
    void refreshSetupState()
  }, [refreshSetupState])

  useEffect(() => {
    if (setupState?.core_ready && !setupRequired && !connected) {
      jarvisClient.connect()
      setConnected(true)
    }
    if (setupRequired && connected) {
      jarvisClient.disconnect()
      setConnected(false)
    }
  }, [setupState?.core_ready, setupRequired, connected])

  const handleSetupComplete = useCallback(async () => {
    const state = await refreshSetupState()
    if (state?.core_ready) {
      setSetupRequired(false)
    }
  }, [refreshSetupState, setSetupRequired])

  if (setupLoading && !setupState) {
    return (
      <SetupStatus
        loading
        title="Starting JARV1S"
        detail="Preparing your local services…"
      />
    )
  }

  if (bootError) {
    return (
      <SetupStatus
        error
        title="Cannot reach JARV1S"
        detail={bootError}
        action={(
          <Button color="brand" onClick={() => void refreshSetupState()}>
            Try again
          </Button>
        )}
      />
    )
  }

  if (setupState && (!setupState.core_ready || setupRequired)) {
    return <SetupWizard initialState={setupState} onComplete={() => void handleSetupComplete()} />
  }

  if (!setupState) {
    return (
      <SetupStatus
        loading
        title="Loading setup"
        detail="Checking your configuration…"
      />
    )
  }

  return <>{children}</>
}
