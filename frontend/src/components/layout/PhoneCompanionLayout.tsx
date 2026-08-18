import React, { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowClockwiseIcon,
  ArrowUpIcon,
  LockKeyIcon,
  MicrophoneIcon,
  PaperPlaneRightIcon,
  StopCircleIcon,
} from '@phosphor-icons/react'
import { jarvisClient } from '../../client/JarvisClient'
import { useJarvisStore } from '../../store/useJarvisStore'
import { cn } from '../../utils/cn'
import { Button } from '../ui/Button'
import { DevicePairingBanner } from './DevicePairingBanner'

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

function isStandalone(): boolean {
  const iosNavigator = navigator as Navigator & { standalone?: boolean }
  return window.matchMedia('(display-mode: standalone)').matches || iosNavigator.standalone === true
}

export const PhoneCompanionLayout: React.FC = () => {
  const connectionState = useJarvisStore((state) => state.connectionState)
  const agentState = useJarvisStore((state) => state.agentState)
  const isSpeaking = useJarvisStore((state) => state.isSpeaking)
  const transcript = useJarvisStore((state) => state.transcript)
  const partialTranscript = useJarvisStore((state) => state.partialTranscript)
  const reconnectAttempt = useJarvisStore((state) => state.reconnectAttempt)
  const microphoneError = useJarvisStore((state) => state.audioDevices.error)
  const addTranscriptItem = useJarvisStore((state) => state.addTranscriptItem)
  const [text, setText] = useState('')
  const [startingTalk, setStartingTalk] = useState(false)
  const [talking, setTalking] = useState(false)
  const [talkLocked, setTalkLocked] = useState(false)
  const [slideProgress, setSlideProgress] = useState(0)
  const [audioError, setAudioError] = useState<string | null>(null)
  const [installPrompt, setInstallPrompt] = useState<BeforeInstallPromptEvent | null>(null)
  const [installDismissed, setInstallDismissed] = useState(
    () => window.localStorage.getItem('jarvis.phone.install-dismissed') === 'true',
  )
  const transcriptEnd = useRef<HTMLDivElement>(null)
  const pressActive = useRef(false)
  const startPending = useRef(false)
  const talkLockedRef = useRef(false)
  const pointerStartY = useRef<number | null>(null)

  const connected = connectionState === 'connected'
  const connecting = connectionState === 'connecting' || connectionState === 'reconnecting'
  const hasCompletedTurn = transcript.some(
    (item) => item.sender === 'assistant' && !item.isPartial && Boolean(item.text?.trim()),
  )
  const showInstall = hasCompletedTurn && !installDismissed && !isStandalone()
  const busy = ['thinking', 'composing_tool', 'running_tool', 'transcribing'].includes(agentState)
  const waitingForWakeWord = talkLocked && agentState === 'idle' && !isSpeaking

  const status = useMemo(() => {
    if (startingTalk) return 'Starting microphone'
    if (isSpeaking || agentState === 'speaking') return 'Speaking'
    if (busy) return 'Thinking'
    if (waitingForWakeWord) return 'Say JARV1S'
    if (talkLocked) return 'Listening'
    if (talking) return 'Listening'
    if (connected) return 'Ready'
    if (connecting) return 'Connecting'
    return 'Offline'
  }, [
    agentState,
    busy,
    connected,
    connecting,
    isSpeaking,
    startingTalk,
    talking,
    talkLocked,
    waitingForWakeWord,
  ])

  useEffect(() => {
    if (useJarvisStore.getState().connectionState === 'disconnected') {
      jarvisClient.connect()
    }
  }, [])

  useEffect(() => {
    transcriptEnd.current?.scrollIntoView({ behavior: 'smooth' })
  }, [transcript.length, partialTranscript])

  useEffect(() => {
    const handleInstallPrompt = (event: Event) => {
      event.preventDefault()
      setInstallPrompt(event as BeforeInstallPromptEvent)
    }
    window.addEventListener('beforeinstallprompt', handleInstallPrompt)
    return () => window.removeEventListener('beforeinstallprompt', handleInstallPrompt)
  }, [])

  const startTalking = async () => {
    if (pressActive.current) return
    pressActive.current = true
    startPending.current = true
    setStartingTalk(true)
    setAudioError(null)
    const result = await jarvisClient.startPushToTalk()
    startPending.current = false
    setStartingTalk(false)
    if (!result.ok) {
      pressActive.current = false
      talkLockedRef.current = false
      pointerStartY.current = null
      setAudioError(result.error)
      setTalking(false)
      setTalkLocked(false)
      setSlideProgress(0)
      return
    }
    if (!pressActive.current) {
      jarvisClient.stopPushToTalk()
      return
    }
    setTalking(true)
  }

  const stopTalking = () => {
    const wasActive = pressActive.current
    pressActive.current = false
    talkLockedRef.current = false
    pointerStartY.current = null
    if (wasActive && !startPending.current) jarvisClient.stopPushToTalk()
    setStartingTalk(false)
    setTalking(false)
    setTalkLocked(false)
    setSlideProgress(0)
  }

  const lockTalking = () => {
    if (!pressActive.current || talkLockedRef.current) return
    talkLockedRef.current = true
    pointerStartY.current = null
    setTalkLocked(true)
    setSlideProgress(1)
    navigator.vibrate?.(20)
  }

  const resumeLockedMicrophone = async () => {
    setStartingTalk(true)
    setAudioError(null)
    const result = await jarvisClient.startPushToTalk()
    setStartingTalk(false)
    if (!result.ok) setAudioError(result.error)
  }

  useEffect(() => {
    if (!connected && pressActive.current) stopTalking()
  }, [connected])

  const sendText = (event: React.FormEvent) => {
    event.preventDefault()
    const value = text.trim()
    if (!value || !connected || busy) return
    addTranscriptItem({
      id: `phone-text-${Date.now()}`,
      text: value,
      sender: 'user',
      type: 'text',
      timestamp: Date.now(),
    })
    jarvisClient.sendTextMessage(value)
    setText('')
  }

  const install = async () => {
    if (!installPrompt) return
    await installPrompt.prompt()
    const choice = await installPrompt.userChoice
    if (choice.outcome === 'accepted') {
      setInstallPrompt(null)
    } else {
      dismissInstall()
    }
  }

  const dismissInstall = () => {
    setInstallDismissed(true)
    window.localStorage.setItem('jarvis.phone.install-dismissed', 'true')
  }

  return (
    <div className="flex h-[100dvh] flex-col overflow-hidden bg-canvas px-5 pb-[calc(1.25rem+env(safe-area-inset-bottom))] pt-[calc(1.25rem+env(safe-area-inset-top))] text-foreground">
      <DevicePairingBanner />

      <header className="flex shrink-0 items-center justify-between">
        <p className="font-mono text-[0.625rem] uppercase tracking-[0.22em] text-foreground-subtle">
          JARV1S
        </p>
        <div
          role="status"
          className="flex min-h-8 items-center gap-2 rounded-full bg-surface/30 px-3 text-xs text-foreground-muted"
        >
          <span
            className={cn(
              'h-2 w-2 rounded-full',
              connected
                ? 'bg-brand-output'
                : connecting
                  ? 'animate-pulse bg-status-warning'
                  : 'bg-status-danger',
            )}
          />
          <span>{status}</span>
        </div>
      </header>

      <main className="mt-6 min-h-0 flex-1 overflow-y-auto pb-8 pr-1">
        {!connected ? (
          <div className="flex h-full min-h-64 items-center justify-center">
            <div className="w-full max-w-sm rounded-panel border border-outline/20 bg-surface/20 p-6 text-center">
              <div
                className={cn(
                  'mx-auto flex h-12 w-12 items-center justify-center rounded-full',
                  connecting
                    ? 'bg-brand/10 text-brand'
                    : 'bg-status-danger/10 text-status-danger',
                )}
              >
                <ArrowClockwiseIcon
                  size={22}
                  className={connecting ? 'animate-spin' : undefined}
                />
              </div>
              <h1 className="mt-4 font-display text-xl text-foreground">
                {connecting ? 'Connecting to your Mac' : 'Can’t reach your Mac'}
              </h1>
              <p className="mt-2 text-sm leading-relaxed text-foreground-muted">
                {reconnectAttempt > 0
                  ? 'JARV1S is retrying. Make sure Tailscale says Connected on this phone and your Mac is awake.'
                  : 'Make sure Tailscale says Connected on this phone and your Mac is awake.'}
              </p>
              {!connecting && (
                <Button
                  size="md"
                  color="brand"
                  className="mt-6 min-h-12 w-full"
                  icon={<ArrowClockwiseIcon size={16} />}
                  onClick={() => jarvisClient.connect()}
                >
                  Try again
                </Button>
              )}
            </div>
          </div>
        ) : transcript.length === 0 && !partialTranscript ? (
          <div className="flex h-full min-h-64 items-center justify-center text-center">
            <div className="max-w-xs">
              <h1 className="font-display text-2xl text-foreground">Hold to talk</h1>
              <p className="mt-2 text-sm leading-relaxed text-foreground-subtle">
                Press and hold the microphone, or type a message below.
              </p>
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {transcript
              .filter((item) => item.type === 'text' || item.type === 'notice')
              .map((item) => (
                <div
                  key={item.id}
                  className={cn(
                    'max-w-[88%] rounded-panel px-4 py-3 text-sm leading-relaxed',
                    item.sender === 'user'
                      ? 'ml-auto bg-brand/15 text-foreground'
                      : 'bg-surface/35 text-foreground-muted',
                  )}
                >
                  {item.text}
                </div>
              ))}
            {partialTranscript && (
              <div className="ml-auto max-w-[88%] rounded-panel bg-brand/10 px-4 py-3 text-sm text-brand">
                {partialTranscript}
              </div>
            )}
            <div ref={transcriptEnd} />
          </div>
        )}
      </main>

      {showInstall && (
        <div className="mb-4 rounded-control bg-surface/30 p-4">
          <p className="text-sm text-foreground">Keep JARV1S one tap away</p>
          <p className="mt-1 text-xs leading-relaxed text-foreground-subtle">
            {installPrompt
              ? 'Install this private companion for faster access.'
              : 'In Safari, tap Share, then Add to Home Screen.'}
          </p>
          <div className="mt-3 flex gap-2">
            {installPrompt && (
              <Button
                size="xs"
                color="brand"
                className="min-h-12"
                onClick={() => void install()}
              >
                Install
              </Button>
            )}
            <Button
              size="xs"
              variant="ghost"
              color="neutral"
              className="min-h-12"
              onClick={dismissInstall}
            >
              {installPrompt ? 'Not now' : 'Got it'}
            </Button>
          </div>
        </div>
      )}

      {connected && (audioError || microphoneError) && (
        <div className="mb-3 flex flex-col items-center gap-2 text-center">
          <p className="text-xs text-status-danger">{audioError || microphoneError}</p>
          {talkLocked && (
            <Button
              size="xs"
              color="brand"
              variant="ghost"
              className="min-h-12"
              icon={<MicrophoneIcon size={16} />}
              onClick={() => void resumeLockedMicrophone()}
            >
              Resume microphone
            </Button>
          )}
        </div>
      )}

      {connected && (
        <div className="shrink-0 space-y-4 border-t border-outline/15 bg-canvas pt-4">
          <div className="flex h-10 items-center justify-center">
            {(talking || startingTalk) && !talkLocked ? (
              <div
                className="flex items-center gap-2 rounded-full border border-brand/20 bg-brand/10 px-3 py-2 text-xs font-medium text-brand transition-all duration-100 ease-out"
                style={{
                  opacity: 0.72 + slideProgress * 0.28,
                  transform: `translateY(${-slideProgress * 12}px) scale(${1 + slideProgress * 0.06})`,
                }}
              >
                <ArrowUpIcon size={14} weight="bold" />
                <span>{slideProgress > 0.7 ? 'Keep sliding' : 'Slide for hands-free'}</span>
                <LockKeyIcon size={14} weight={slideProgress > 0.7 ? 'fill' : 'regular'} />
              </div>
            ) : talkLocked ? (
              <div
                className={cn(
                  'flex items-center gap-2 rounded-full px-3 py-2 text-xs font-medium',
                  waitingForWakeWord
                    ? 'bg-brand/10 text-brand'
                    : 'bg-brand-output/10 text-brand-output',
                )}
              >
                <span className="relative flex h-2 w-2">
                  {!waitingForWakeWord && (
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-output opacity-40" />
                  )}
                  <span
                    className={cn(
                      'relative inline-flex h-2 w-2 rounded-full',
                      waitingForWakeWord ? 'bg-brand' : 'bg-brand-output',
                    )}
                  />
                </span>
                <LockKeyIcon size={14} weight="fill" />
                <span>{waitingForWakeWord ? 'Hands-free · say JARV1S' : 'Hands-free · listening'}</span>
              </div>
            ) : null}
          </div>

          <div
            className="flex justify-center transition-transform duration-200 ease-out"
            style={{
              transform:
                (talking || startingTalk) && !talkLocked
                  ? `translateY(${-slideProgress * 12}px)`
                  : undefined,
            }}
          >
            <div className="relative">
              {talkLocked && (
                <span
                  aria-hidden="true"
                  className={cn(
                    'absolute -inset-2 rounded-full border',
                    waitingForWakeWord
                      ? 'border-brand/30'
                      : 'animate-pulse border-brand-output/40',
                  )}
                />
              )}
              <button
                type="button"
                className={cn(
                  'relative flex h-20 w-20 touch-none select-none flex-col items-center justify-center gap-1 rounded-full border transition-all duration-200 ease-out focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/60',
                  talkLocked
                    ? waitingForWakeWord
                      ? 'scale-105 border-brand/60 bg-brand/15 text-brand shadow-glow-brand-tight'
                      : 'scale-105 border-brand-output/80 bg-brand-output/25 text-brand-output shadow-glow-output'
                    : talking
                      ? 'scale-105 border-brand-output bg-brand-output/20 text-brand-output shadow-glow-brand-tight'
                      : 'border-brand/50 bg-brand/15 text-brand',
                  (!connected || (busy && !talkLocked)) && 'cursor-not-allowed opacity-40',
                )}
                disabled={!connected || (busy && !talkLocked)}
                aria-label={
                  talkLocked
                    ? 'Exit hands-free listening'
                    : talking
                      ? 'Release to send'
                      : 'Hold to talk'
                }
                aria-pressed={talking}
                onPointerDown={(event) => {
                  if (talkLockedRef.current) {
                    event.preventDefault()
                    stopTalking()
                    return
                  }
                  if (talking || startingTalk) return
                  pointerStartY.current = event.clientY
                  talkLockedRef.current = false
                  setTalkLocked(false)
                  setSlideProgress(0)
                  event.currentTarget.setPointerCapture(event.pointerId)
                  void startTalking()
                }}
                onPointerMove={(event) => {
                  if (
                    !pressActive.current
                    || pointerStartY.current === null
                    || talkLockedRef.current
                  ) {
                    return
                  }
                  const progress = Math.min(
                    1,
                    Math.max(0, pointerStartY.current - event.clientY) / 64,
                  )
                  setSlideProgress(progress)
                  if (progress >= 1) lockTalking()
                }}
                onPointerUp={() => {
                  pointerStartY.current = null
                  if (pressActive.current && !talkLockedRef.current) stopTalking()
                }}
                onPointerCancel={() => {
                  if (!talkLockedRef.current) stopTalking()
                }}
                onLostPointerCapture={() => {
                  if (!talkLockedRef.current) stopTalking()
                }}
                onKeyDown={(event) => {
                  if (event.repeat || (event.key !== ' ' && event.key !== 'Enter')) return
                  event.preventDefault()
                  if (talkLockedRef.current) {
                    stopTalking()
                  } else if (!talking) {
                    void startTalking()
                  }
                }}
                onKeyUp={(event) => {
                  if (
                    pressActive.current
                    && !talkLockedRef.current
                    && (event.key === ' ' || event.key === 'Enter')
                  ) {
                    stopTalking()
                  }
                }}
              >
                <MicrophoneIcon size={34} weight={talking ? 'fill' : 'regular'} />
                <span className="type-label-small">
                  {talkLocked ? 'End' : 'Hold'}
                </span>
              </button>
            </div>
          </div>

          <p className="text-center type-meta text-foreground-subtle">
            {startingTalk
              ? 'Waiting for microphone access…'
              : talkLocked
                ? waitingForWakeWord
                  ? 'Say “JARV1S” for another turn · tap the microphone to end'
                  : 'Pause to send automatically · tap the microphone to end'
                : talking
                  ? 'Release to send'
                  : 'Hold to talk · slide up for hands-free'}
          </p>

          {(isSpeaking || busy) && (
            <div className="flex justify-center">
              <Button
                size="sm"
                color="critical"
                variant="ghost"
                className="min-h-12"
                icon={<StopCircleIcon size={24} />}
                aria-label="Stop response"
                onClick={() => jarvisClient.stopPlayback()}
              >
                Stop response
              </Button>
            </div>
          )}

          <label htmlFor="phone-text-message" className="block text-xs text-foreground-muted">
            Message
          </label>
          <form
            onSubmit={sendText}
            className="flex min-h-12 items-center gap-2 rounded-panel bg-surface/30 px-3"
          >
            <input
              id="phone-text-message"
              className="min-w-0 flex-1 bg-transparent px-1 text-base text-foreground placeholder:text-foreground-disabled focus:outline-none focus:ring-0"
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="Type a message"
              disabled={!connected || busy}
            />
            <Button
              type="submit"
              size="icon-sm"
              variant="ghost"
              color="action"
              className="min-h-12 min-w-12"
              icon={<PaperPlaneRightIcon size={18} />}
              aria-label="Send message"
              disabled={!text.trim() || !connected || busy}
            />
          </form>
        </div>
      )}
    </div>
  )
}
