import React from 'react'
import { StatusBar } from './StatusBar'
import { ControlBar } from './ControlBar'
import { PrimaryCanvas } from './PrimaryCanvas'
import { SetupGate } from '../features/setup/SetupGate'
import { DevicePairingBanner } from './DevicePairingBanner'
import { PhoneCompanionLayout } from './PhoneCompanionLayout'
import { isPhoneCompanion } from '../../runtime/clientSurface'

export const RootLayout: React.FC = () => {
  if (isPhoneCompanion()) {
    return <PhoneCompanionLayout />
  }

  return (
    <SetupGate>
      {/* Stage master: full-screen viewport. */}
      <div className="stage-background relative h-screen w-screen overflow-hidden text-foreground">

        {/* ZONE 2: PRIMARY CANVAS (Layer 10 - background) */}
        <main className="absolute inset-0 z-10 flex flex-col">
          <DevicePairingBanner />
          <PrimaryCanvas />
        </main>

        {/* ZONE 1: STATUS BAR (Layer 65 - above modal backdrop; manages its own pointer-events internally) */}
        <div className="absolute top-0 w-full z-[65]">
          <StatusBar />
        </div>

        {/* ZONE 3: CONTROL BAR (Layer 65 - above modal backdrop) */}
        <div className="absolute bottom-0 w-full z-[65]">
          <ControlBar />
        </div>
      </div>
    </SetupGate>
  )
}
