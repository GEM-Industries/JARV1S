import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'

const root = document.querySelector('.startup')
const headlineEl = document.getElementById('headline')
const milestoneEl = document.getElementById('milestone')
const elapsedEl = document.getElementById('elapsed')
const trackEl = document.getElementById('track')
const detailsEl = document.getElementById('details')
const bootLog = document.getElementById('boot-log')
const detailEl = document.getElementById('detail')
const retryBtn = document.getElementById('retry')
const openLogsBtn = document.getElementById('open-logs')

const TERMINAL_STATES = ['ready', 'needs_setup', 'degraded']
const ELAPSED_REVEAL_MS = 10_000

const MILESTONES = [
  {
    id: 'prepare',
    label: 'Checking this Mac',
    phases: ['check_prerequisites', 'prepare_dependencies'],
  },
  {
    id: 'services',
    label: 'Preparing local services',
    phases: ['start_services'],
  },
  {
    id: 'host',
    label: 'Starting the assistant',
    phases: ['start_backend'],
  },
  {
    id: 'ready',
    label: 'Confirming readiness',
    phases: ['wait_for_health', 'resolve_setup_state', 'ready'],
  },
]

const PHASE_TO_MILESTONE = Object.fromEntries(
  MILESTONES.flatMap((milestone, index) =>
    milestone.phases.map((phase) => [phase, index]),
  ),
)

const timeline = []
let lastStateKey = ''
let startedAt = Date.now()
let elapsedTimer = null
let lastActiveIndex = 0

export function milestoneIndexForPhase(phase) {
  if (phase === 'failed') return -1
  return PHASE_TO_MILESTONE[phase] ?? 0
}

export function milestoneLabelForPhase(phase, state) {
  if (state === 'failed' || phase === 'failed') {
    return 'Startup failed'
  }
  const index = milestoneIndexForPhase(phase)
  return MILESTONES[Math.max(0, index)]?.label ?? 'Starting JARV1S'
}

function formatElapsed(ms) {
  const seconds = Math.max(0, Math.floor(ms / 1000))
  return `${seconds}s`
}

function recordState(state) {
  const children = Array.isArray(state.children) ? state.children : []
  const key = JSON.stringify([
    state.phase,
    state.state,
    state.message,
    state.detail,
    children,
    state.updated_at,
  ])
  if (key === lastStateKey) return
  lastStateKey = key
  timeline.push({ ...state, children })
  if (timeline.length > 16) timeline.shift()
}

function renderTrack(activeIndex, failed) {
  const steps = trackEl.querySelectorAll('.track-step')
  steps.forEach((step, index) => {
    step.classList.remove('pending', 'active', 'complete', 'failed')
    step.removeAttribute('aria-current')
    if (failed && index === activeIndex) {
      step.classList.add('failed')
    } else if (index < activeIndex) {
      step.classList.add('complete')
    } else if (index === activeIndex && !failed) {
      step.classList.add('active')
      step.setAttribute('aria-current', 'step')
    } else if (failed && index > activeIndex) {
      step.classList.add('pending')
    } else {
      step.classList.add('pending')
    }
  })
}

function renderBootLog(state) {
  recordState(state)
  const settled = state.state === 'failed' || TERMINAL_STATES.includes(state.state)
  const lines = timeline.flatMap((snapshot) => {
    const entries = [{
      text: `${snapshot.phase} · ${snapshot.state} · ${snapshot.message || 'Starting JARV1S'}`,
      kind: 'main',
    }]
    for (const child of snapshot.children) {
      const detail = child.detail ? ` · ${child.detail}` : ''
      entries.push({
        text: `${child.name} · ${child.status}${detail}`,
        kind: 'detail',
      })
    }
    if (snapshot.detail) {
      entries.push({ text: snapshot.detail, kind: 'detail' })
    }
    return entries
  }).slice(-14)

  bootLog.replaceChildren(
    ...lines.map((entry, index) => {
      const isLast = index === lines.length - 1
      const line = document.createElement('div')
      line.className = 'log-line'
      if (isLast && state.state === 'failed') line.classList.add('failed')
      else if (isLast && !settled) line.classList.add('current')
      line.textContent = entry.text
      return line
    }),
  )
}

function updateElapsed() {
  const ms = Date.now() - startedAt
  if (ms < ELAPSED_REVEAL_MS) {
    elapsedEl.classList.add('hidden')
    elapsedEl.textContent = ''
    return
  }
  elapsedEl.classList.remove('hidden')
  elapsedEl.textContent = `${formatElapsed(ms)} elapsed`
}

function startElapsedTimer() {
  if (elapsedTimer) return
  updateElapsed()
  elapsedTimer = window.setInterval(updateElapsed, 1000)
}

function stopElapsedTimer() {
  if (!elapsedTimer) return
  window.clearInterval(elapsedTimer)
  elapsedTimer = null
}

function applyState(state) {
  const failed = state.state === 'failed'
  const mappedIndex = milestoneIndexForPhase(state.phase)
  if (mappedIndex >= 0) {
    lastActiveIndex = mappedIndex
  }
  const activeIndex = lastActiveIndex

  root.classList.toggle('failed', failed)
  root.setAttribute('aria-busy', failed || TERMINAL_STATES.includes(state.state) ? 'false' : 'true')

  headlineEl.textContent = failed
    ? (state.message || 'JARV1S could not start')
    : 'Starting JARV1S'
  milestoneEl.textContent = failed
    ? 'Startup failed'
    : milestoneLabelForPhase(state.phase, state.state)

  renderTrack(activeIndex, failed)
  renderBootLog(state)
  detailEl.textContent = failed ? (state.detail ?? '') : ''

  retryBtn.classList.toggle('hidden', !failed)
  openLogsBtn.classList.toggle('hidden', !failed)
  if (failed) {
    detailsEl.open = true
    stopElapsedTimer()
    updateElapsed()
  } else {
    startElapsedTimer()
  }

  if (state.backend_url && TERMINAL_STATES.includes(state.state)) {
    stopElapsedTimer()
    window.location.replace(state.backend_url)
  }
}

async function main() {
  retryBtn.addEventListener('click', () => {
    retryBtn.classList.add('hidden')
    openLogsBtn.classList.add('hidden')
    root.classList.remove('failed')
    detailsEl.open = false
    timeline.length = 0
    lastStateKey = ''
    lastActiveIndex = 0
    bootLog.replaceChildren()
    startedAt = Date.now()
    startElapsedTimer()
    void invoke('restart_host')
  })
  openLogsBtn.addEventListener('click', () => {
    void invoke('open_logs_folder')
  })

  startElapsedTimer()

  await listen('host-launch-update', (event) => {
    applyState(event.payload)
  })

  try {
    const initial = await invoke('get_launch_state')
    applyState(initial)
  } catch (error) {
    applyState({
      phase: 'failed',
      state: 'failed',
      message: 'Could not read startup state.',
      detail: String(error),
    })
  }

  void invoke('start_host').catch((error) => {
    applyState({
      phase: 'failed',
      state: 'failed',
      message: 'JARV1S could not start.',
      detail: String(error),
    })
  })
}

void main()
