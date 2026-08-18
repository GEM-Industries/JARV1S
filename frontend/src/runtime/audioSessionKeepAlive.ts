/**
 * Keep WKWebView / Tauri CoreAudio output from going dormant.
 *
 * After prolonged background/idle (and some audio-session interruptions),
 * macOS can tear down the webview's native output session. WebAudio then
 * reports state=running and buffer onended fires, but no sound reaches the
 * speakers — and recreating AudioContext / JS reload does not recover it.
 * Only a full app relaunch (or keeping a real HTMLAudioElement playing
 * silent PCM) restores output. See Voicebox PR #486 / WebKit session teardown.
 *
 * Uses zero-PCM WAV at full volume (not muted) so WebKit cannot optimize
 * the element away. Desktop-only; mount once at app lifetime.
 */

const SAMPLE_RATE = 8000
const DURATION_S = 1

function buildSilentWavBlobUrl(durationS = DURATION_S, sampleRate = SAMPLE_RATE): string {
  const numSamples = sampleRate * durationS
  const dataSize = numSamples * 2
  const buffer = new ArrayBuffer(44 + dataSize)
  const view = new DataView(buffer)
  const writeStr = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i))
  }
  writeStr(0, 'RIFF')
  view.setUint32(4, 36 + dataSize, true)
  writeStr(8, 'WAVE')
  writeStr(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeStr(36, 'data')
  view.setUint32(40, dataSize, true)
  // PCM samples left at 0 (silence).
  return URL.createObjectURL(new Blob([buffer], { type: 'audio/wav' }))
}

/**
 * Start a process-lifetime silent HTML audio loop to hold the OS audio session.
 * Returns a cleanup that pauses the element and removes listeners.
 */
export function startAudioSessionKeepAlive(): () => void {
  if (typeof window === 'undefined' || typeof Audio === 'undefined') {
    return () => {}
  }

  const url = buildSilentWavBlobUrl()
  const el = new Audio(url)
  el.loop = true
  el.volume = 1
  el.preload = 'auto'

  let gestureBound = false

  const tryPlay = () => {
    if (!el.paused) return
    void el
      .play()
      .then(() => {
        if (!gestureBound) return
        window.removeEventListener('pointerdown', onGesture)
        window.removeEventListener('keydown', onGesture)
        gestureBound = false
      })
      .catch(() => {
        // Autoplay may be blocked until a user gesture; listeners retry.
      })
  }

  const onGesture = () => tryPlay()
  const onWake = () => {
    if (document.visibilityState === 'hidden') return
    tryPlay()
  }

  document.addEventListener('visibilitychange', onWake)
  window.addEventListener('focus', onWake)
  window.addEventListener('pageshow', onWake)

  tryPlay()
  if (el.paused) {
    gestureBound = true
    window.addEventListener('pointerdown', onGesture)
    window.addEventListener('keydown', onGesture)
  }

  return () => {
    document.removeEventListener('visibilitychange', onWake)
    window.removeEventListener('focus', onWake)
    window.removeEventListener('pageshow', onWake)
    if (gestureBound) {
      window.removeEventListener('pointerdown', onGesture)
      window.removeEventListener('keydown', onGesture)
      gestureBound = false
    }
    el.pause()
    el.src = ''
    URL.revokeObjectURL(url)
  }
}
