import React, { useState, useRef, useCallback } from 'react';
import { MicrophoneIcon, MicrophoneSlashIcon, StopCircleIcon, WifiHighIcon, PaperPlaneRight, Paperclip, X } from '@phosphor-icons/react';
import { useJarvisStore } from '../../store/useJarvisStore';
import { jarvisClient } from '../../client/JarvisClient';
import { getSystemStatus } from '../../config/systemStatus';
import { useVoiceEntry } from '../../features/voice/useVoiceEntry';
import { Button } from '../ui/Button';
import { cn } from '../../utils/cn';

const ALLOWED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];
const MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024;
const BUSY_STATES = new Set(['thinking', 'speaking', 'composing_tool', 'running_tool', 'transcribing']);

export const ControlBar: React.FC = () => {
  const connectionState = useJarvisStore((state) => state.connectionState);
  const hostState = useJarvisStore((state) => state.hostState);
  const agentState = useJarvisStore((state) => state.agentState);
  const reconnectAttempt = useJarvisStore((state) => state.reconnectAttempt);
  const isSpeaking = useJarvisStore((state) => state.isSpeaking);
  const pendingAttachment = useJarvisStore((state) => state.pendingAttachment);
  const setPendingAttachment = useJarvisStore((state) => state.setPendingAttachment);
  const voice = useVoiceEntry();
  
  const isConnected = connectionState === 'connected';
  const status = getSystemStatus(hostState, connectionState, agentState, reconnectAttempt);

  const isBusy = BUSY_STATES.has(agentState) || isSpeaking;
  const isVoiceActive = agentState === 'listening' || agentState === 'waking';
  const showStop = isConnected && isBusy;

  const addTranscriptItem = useJarvisStore((state) => state.addTranscriptItem);

  const [textInput, setTextInput] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const processImageFile = useCallback((file: File) => {
    if (!ALLOWED_IMAGE_TYPES.includes(file.type)) return;
    if (file.size > MAX_ATTACHMENT_SIZE) return;

    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = reader.result as string;
      const base64 = dataUrl.split(',')[1];
      setPendingAttachment({ dataUrl, base64, mimeType: file.type });
      jarvisClient.sendMessage('user_attachment', { data: base64, mime_type: file.type });
    };
    reader.readAsDataURL(file);
  }, [setPendingAttachment]);

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const file = item.getAsFile();
        if (file) processImageFile(file);
        return;
      }
    }
  }, [processImageFile]);

  const handleTextSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = textInput.trim();
    const hasImage = !!pendingAttachment;
    if ((!trimmed && !hasImage) || !isConnected || isBusy) return;
    if (!trimmed && hasImage && isVoiceActive) return;

    const consumed = useJarvisStore.getState().consumePendingAttachment();
    const attachments = consumed
      ? [{ type: 'image', url: consumed.dataUrl }]
      : undefined;

    addTranscriptItem({
      id: `text-${Date.now()}`,
      text: trimmed || '',
      sender: 'user',
      type: 'text',
      timestamp: Date.now(),
      attachments,
    });

    jarvisClient.sendTextMessage(trimmed);
    setTextInput('');
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const handleMainAction = () => {
    if (connectionState === 'disconnected' || connectionState === 'reconnecting' || connectionState === 'error') {
      jarvisClient.connect();
      return;
    }
    voice.onPrimaryAction();
  };

  const handleStop = () => {
    jarvisClient.stopPlayback();
  };

  const getMainButtonConfig = () => {
    if (connectionState !== 'connected') {
      return {
        text: status.shortLabel || status.label,
        color: status.buttonColor,
        icon: <WifiHighIcon size={18} className={cn(status.pulse && "animate-pulse")} />,
        className: "min-w-[140px]",
        disabled: false,
      };
    }

    const { action, text, tone, disabled } = voice.presentation;
    const slash = action === 'resume' || action === 'setup' || action === 'retry' || action === 'download';

    return {
      text,
      color: tone,
      icon: slash
        ? <MicrophoneSlashIcon size={18} weight={action === 'resume' ? 'fill' : 'regular'} />
        : <MicrophoneIcon size={18} weight={action === 'mute' ? 'fill' : 'regular'} className={cn(action === 'preparing' && 'animate-pulse')} />,
      className: "min-w-[140px]",
      disabled,
    };
  };

  const config = getMainButtonConfig();

  return (
    <div className="w-full z-50 relative select-none pointer-events-none">
      <div className="absolute inset-x-0 bottom-0 h-safe-bottom bg-gradient-to-t from-canvas via-canvas/98 to-transparent" />

      <div className="relative mx-auto flex w-full max-w-4xl flex-col items-stretch gap-3 px-6 pb-6 sm:flex-row sm:items-end">
        <div className="relative min-w-0 flex-1 pointer-events-auto">
          {pendingAttachment && (
            <div className="flex flex-col items-start gap-1 mb-2 ml-1">
              <div className="relative">
                <img
                  src={pendingAttachment.dataUrl}
                  alt="Pending attachment"
                  className={cn(
                    "h-16 rounded-control border object-cover",
                    isVoiceActive ? "border-brand/40" : "border-outline"
                  )}
                />
                <button
                  type="button"
                  onClick={() => {
                    setPendingAttachment(null);
                    jarvisClient.sendMessage('user_attachment', { clear: true });
                  }}
                  className="absolute -top-2 -right-2 w-6 h-6 rounded-full bg-surface border border-outline flex items-center justify-center hover:bg-status-danger/20 transition-colors"
                >
                  <X size={10} weight="bold" className="text-foreground-muted" />
                </button>
              </div>
              {isVoiceActive && (
                <span className="text-xs text-brand/70 font-body tracking-wide">
                  Sending with voice
                </span>
              )}
            </div>
          )}

          <form onSubmit={handleTextSubmit} className={cn(
            "flex items-center gap-1.5 rounded-full border px-2 py-1",
            "bg-transparent border-outline",
            "focus-within:bg-surface/20",
            "focus-within:border-brand/40",
            "transition-colors duration-200",
            !isConnected && "opacity-40 pointer-events-none"
          )}>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/jpeg,image/png,image/webp,image/gif"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) processImageFile(file);
                e.target.value = '';
              }}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={!isConnected}
              className={cn(
                "w-9 h-9 rounded-full flex items-center justify-center shrink-0",
                "text-foreground-disabled hover:text-foreground-muted",
                "disabled:opacity-40 disabled:cursor-not-allowed",
                "transition-colors duration-200"
              )}
            >
              <Paperclip size={16} />
            </button>
            <input
              ref={inputRef}
              type="text"
              value={textInput}
              onChange={(e) => setTextInput(e.target.value)}
              onPaste={handlePaste}
              placeholder="Type a message..."
              disabled={!isConnected}
              className={cn(
                "flex-1 bg-transparent py-1.5",
                "text-sm text-foreground placeholder:text-foreground-subtle font-body tracking-wide",
                "focus:outline-none focus:ring-0 focus:shadow-none",
                "focus-visible:ring-0 focus-visible:shadow-none focus-visible:ring-offset-0",
              )}
            />
            <button
              type="submit"
              className={cn(
                "w-9 h-9 rounded-full flex items-center justify-center shrink-0",
                "transition-all duration-200",
                (textInput.trim() || (pendingAttachment && !isVoiceActive)) && !isBusy
                  ? "text-brand hover:text-status-success opacity-100 scale-100"
                  : "text-foreground-disabled opacity-0 scale-90 pointer-events-none"
              )}
            >
              <PaperPlaneRight size={16} weight="fill" />
            </button>
          </form>

          {isConnected && voice.presentation.detail && (
            <p
              className="mt-2 ml-3 max-w-md type-meta text-foreground-subtle sm:absolute sm:left-0 sm:top-full"
              role="status"
            >
              {voice.presentation.detail}
              {(voice.presentation.action === 'setup') && (
                <>
                  {' '}
                  <button
                    type="button"
                    className="text-brand hover:underline"
                    onClick={() => voice.openVoiceSettings()}
                  >
                    Open Voice & Audio
                  </button>
                </>
              )}
            </p>
          )}
        </div>

        <div className="pointer-events-auto flex shrink-0 items-center justify-end gap-3">
          <Button
            onClick={handleMainAction}
            color={config.color}
            size="md"
            disabled={config.disabled}
            className={cn("min-w-[160px]", config.className)}
            icon={config.icon}
          >
            <span className="font-medium tracking-wider">
              {config.text}
            </span>
          </Button>

          {showStop && (
            <Button
              onClick={handleStop}
              color="critical"
              size="md"
              className="w-11 h-11 rounded-full px-0 flex items-center justify-center shadow-glow-danger"
              aria-label="Stop"
            >
              <StopCircleIcon size={20} weight="fill" />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
