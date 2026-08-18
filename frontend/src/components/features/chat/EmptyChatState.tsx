import React from 'react'
import { MicrophoneIcon } from '@phosphor-icons/react'
import { jarvisClient } from '../../../client/JarvisClient'
import { START_VOICE_LABEL } from '../../../features/voice/voiceEntry'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { Button } from '../../ui/Button'

const STARTERS: Array<{ label: string; prompt: string }> = [
  { label: 'What can you help with?', prompt: 'What can you help me with?' },
  { label: "What's the weather?", prompt: "What's the weather like?" },
  { label: 'Help me plan my day', prompt: 'Help me plan my day.' },
]

/**
 * Empty home orientation. Typing works immediately; voice starts from the footer control.
 */
export const EmptyChatState: React.FC = () => {
  const connectionState = useJarvisStore((s) => s.connectionState)
  const addTranscriptItem = useJarvisStore((s) => s.addTranscriptItem)
  const isTranscriptVisible = useJarvisStore((s) => s.isTranscriptVisible)
  const toggleTranscript = useJarvisStore((s) => s.toggleTranscript)
  const connected = connectionState === 'connected'

  const sendStarter = (prompt: string) => {
    if (!connected) return
    addTranscriptItem({
      id: `text-${Date.now()}`,
      text: prompt,
      sender: 'user',
      type: 'text',
      timestamp: Date.now(),
    })
    jarvisClient.sendTextMessage(prompt)
    if (!isTranscriptVisible) toggleTranscript()
  }

  return (
    <div className="pointer-events-none flex w-full items-center justify-center">
      <div className="pointer-events-auto w-full max-w-md space-y-6 text-center">
        <div className="space-y-2">
          <h2 className="font-display text-2xl text-foreground">
            What would you like help with?
          </h2>
          <p className="text-sm leading-relaxed text-foreground-muted">
            Type below anytime. Or use{' '}
            <span className="inline-flex items-center gap-1 font-medium text-foreground">
              <MicrophoneIcon size={14} className="text-brand" aria-hidden />
              {START_VOICE_LABEL}
            </span>{' '}
            when you want to talk — the microphone stays off until then.
          </p>
        </div>

        <div className="flex flex-col gap-2">
          {STARTERS.map((item) => (
            <Button
              key={item.prompt}
              size="md"
              color="subtle"
              variant="ghost"
              disabled={!connected}
              className="w-full justify-start"
              onClick={() => sendStarter(item.prompt)}
            >
              {item.label}
            </Button>
          ))}
        </div>
      </div>
    </div>
  )
}
