import React, { useEffect } from 'react'
import { RootLayout } from './components/layout/RootLayout'
import { startAudioSessionKeepAlive } from './runtime/audioSessionKeepAlive'
import { isDesktopApp } from './runtime/clientSurface'
import { startHostStateSync } from './runtime/hostLifecycle'

export const App: React.FC = () => {
  useEffect(() => {
    let disposed = false
    let unlisten: (() => void) | null = null
    void startHostStateSync().then((cleanup) => {
      if (disposed) cleanup()
      else unlisten = cleanup
    })
    return () => {
      disposed = true
      unlisten?.()
    }
  }, [])

  // Desktop WKWebView: hold CoreAudio open across background/idle so TTS
  // WebAudio does not render silently. Independent of mic capture lifecycle.
  useEffect(() => {
    if (!isDesktopApp()) return
    return startAudioSessionKeepAlive()
  }, [])

  return <RootLayout />
}
