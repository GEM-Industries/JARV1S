import React from 'react'
import { SpinnerIcon } from '@phosphor-icons/react'
import { PairingCommand } from './PairingCommand'
import { showSpeakerPairCommand, type LanPairStatus } from './pairing'

export const SpeakerPairStatus: React.FC<{
  lanStatus: LanPairStatus
  waiting: boolean
  connected: boolean
  command: string
  expiresAt: string
  onRenew?: () => void
  renewing?: boolean
}> = ({ lanStatus, waiting, connected, command, expiresAt, onRenew, renewing }) => {
  if (connected) return null
  const connecting = lanStatus === 'connecting'
  const showCommand = showSpeakerPairCommand(lanStatus, { connected, waiting })
  const status = connecting
    ? 'Connecting from this Mac…'
    : lanStatus === 'failed'
      ? 'Could not reach the speaker from this Mac'
      : waiting
        ? 'Waiting for the speaker to connect…'
        : 'Speaker has not connected yet'
  return (
    <div className="flex flex-col gap-4">
      <p className="flex items-center gap-2 type-body text-foreground-muted" role="status">
        {connecting || (waiting && lanStatus !== 'failed') ? (
          <SpinnerIcon className="animate-spin" size={14} />
        ) : null}
        {status}
      </p>
      {showCommand && (
        <PairingCommand
          command={command}
          expiresAt={expiresAt}
          onRenew={onRenew}
          renewing={renewing}
        />
      )}
    </div>
  )
}
