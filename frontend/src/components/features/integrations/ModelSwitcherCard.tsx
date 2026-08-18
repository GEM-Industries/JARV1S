import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { CaretDownIcon, CaretRightIcon, SpinnerIcon, WarningIcon } from '@phosphor-icons/react'
import {
  setupApi,
  SetupApiError,
  type ConfigureLlmRequest,
  type LocalLlmRuntime,
  type LlmProviderOption,
  type ManagedLlmStatus,
  type SetupState,
  type ValidationResult,
} from '../../../client/setupApi'
import { isDesktopApp } from '../../../runtime/clientSurface'
import { setManagedLocalLlmEnabled } from '../../../runtime/desktopBridge'
import { ensureManagedLocalReady, ManagedLocalDownloadPausedError, type ManagedLocalPhase } from '../../../client/managedLocalLlm'
import { useJarvisStore } from '../../../store/useJarvisStore'
import {
  formatModelLabel,
  formatProviderLabel,
  matchActivePreset,
  MODEL_PRESETS,
  type ModelPresetId,
} from '../../../constants/modelPresets'
import { cn } from '../../../utils/cn'
import { Button } from '../../ui/Button'
import { FieldControl, Input } from '../../ui/FieldControl'
import { PanelSection } from '../../ui/PanelSection'
import { Select } from '../../ui/Select'
import { StatusDot } from '../../ui/StatusDot'
import { StatusPill } from '../../ui/StatusPill'

interface ModelSwitcherCardProps {
  active: boolean
}

function presetProvider(presetId: ModelPresetId, providers: LlmProviderOption[]): LlmProviderOption | undefined {
  const preset = MODEL_PRESETS.find((item) => item.id === presetId)
  if (!preset) return undefined
  return providers.find((provider) => provider.id === preset.provider)
}

function formatDownloadBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 MB'
  if (bytes < 1024 ** 3) {
    const mb = bytes / 1024 ** 2
    return `${mb >= 100 ? mb.toFixed(0) : mb.toFixed(1)} MB`
  }
  const gb = bytes / 1024 ** 3
  return `${gb >= 10 ? gb.toFixed(0) : gb.toFixed(1)} GB`
}

function downloadProgressPct(completed: number, total: number): number {
  if (total <= 0) return 0
  return Math.min(99, Math.max(0, Math.round((completed / total) * 100)))
}

function presetModel(presetId: ModelPresetId, providers: LlmProviderOption[]): string {
  const preset = MODEL_PRESETS.find((item) => item.id === presetId)
  if (!preset) return ''
  if (preset.model) return preset.model
  return presetProvider(presetId, providers)?.default_model ?? ''
}

export const ModelSwitcherCard: React.FC<ModelSwitcherCardProps> = ({ active }) => {
  const setSetupState = useJarvisStore((s) => s.setSetupState)

  const [setupState, setLocalSetupState] = useState<SetupState | null>(null)
  const [providers, setProviders] = useState<LlmProviderOption[]>([])
  const [managed, setManaged] = useState<ManagedLlmStatus | null>(null)
  const [localRuntimes, setLocalRuntimes] = useState<LocalLlmRuntime[]>([])
  const [loading, setLoading] = useState(false)
  const [busyAction, setBusyAction] = useState<ModelPresetId | 'advanced' | 'remove' | 'cancel' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const [failedConfig, setFailedConfig] = useState<ConfigureLlmRequest | null>(null)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [keyPreset, setKeyPreset] = useState<ModelPresetId | null>(null)
  const [apiKey, setApiKey] = useState('')
  const [advancedProvider, setAdvancedProvider] = useState('openrouter')
  const [advancedModel, setAdvancedModel] = useState('')
  const [advancedBaseUrl, setAdvancedBaseUrl] = useState('')
  const [selectedLocalRuntime, setSelectedLocalRuntime] = useState<LocalLlmRuntime | null>(null)
  const [selectedLocalModel, setSelectedLocalModel] = useState('')
  const [scanHint, setScanHint] = useState<string | null>(null)
  const [localPhase, setLocalPhase] = useState<ManagedLocalPhase | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [state, providerList, managedStatus] = await Promise.all([
        setupApi.getState(),
        setupApi.listProviders(),
        setupApi.getManagedLocalStatus().catch(() => null),
      ])
      setLocalSetupState(state)
      setProviders(providerList)
      setManaged(managedStatus)
      setAdvancedProvider(state.llm.provider || providerList[0]?.id || 'openrouter')
      setAdvancedModel(state.llm.model || providerList.find((p) => p.id === state.llm.provider)?.default_model || '')
      const activePreset = matchActivePreset(
        state.llm.provider,
        state.llm.model,
        providerList,
        managedStatus?.model_id,
      )
      if (activePreset === 'local') {
        setSelectedLocalModel(state.llm.model || '')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load model settings.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (active) void load()
  }, [active, load])

  const activePresetId = useMemo(
    () => matchActivePreset(
      setupState?.llm.provider,
      setupState?.llm.model,
      providers,
      managed?.model_id,
    ),
    [managed?.model_id, providers, setupState?.llm.model, setupState?.llm.provider],
  )

  const isManagedActive = activePresetId === 'local'

  const activeSummary = useMemo(() => {
    if (!setupState?.llm) return null
    if (isManagedActive) {
      return managed?.model_label || formatModelLabel(setupState.llm.model)
    }
    return `${formatProviderLabel(setupState.llm.provider)} · ${formatModelLabel(setupState.llm.model)}`
  }, [isManagedActive, managed?.model_label, setupState?.llm])

  const activeSubtitle = useMemo(() => {
    if (isManagedActive) return 'Private on this Mac · no API key'
    if (!activePresetId) {
      if (['ollama', 'lmstudio', 'llamacpp'].includes(setupState?.llm.provider ?? '')) {
        return 'Your own local server'
      }
      return 'Custom configuration'
    }
    return MODEL_PRESETS.find((preset) => preset.id === activePresetId)?.subtitle ?? 'Custom configuration'
  }, [activePresetId, isManagedActive, setupState?.llm.provider])

  const applyConfig = async (body: {
    provider: string
    model?: string
    base_url?: string
    api_key?: string
  }) => {
    const result = await setupApi.activateLlm(body)
    if (!result.core_ready) {
      throw new Error(result.message)
    }
    // Keep the previous managed runtime alive until activation succeeds so the
    // backend can roll back to it if validation or initialization fails.
    if (isDesktopApp()) {
      await setManagedLocalLlmEnabled(false)
    }
    setLocalSetupState(result.state)
    setSetupState(result.state)
    const managedStatus = await setupApi.getManagedLocalStatus().catch(() => null)
    setManaged(managedStatus)
    return result.state
  }

  const isByoLocalProvider = (providerId: string) =>
    ['ollama', 'lmstudio', 'llamacpp'].includes(providerId)

  const formatCaughtError = (cause: unknown, fallback: string): string => {
    if (cause instanceof SetupApiError) {
      if (cause.validation?.message) {
        return cause.validation.next_action
          ? `${cause.validation.message} ${cause.validation.next_action}`
          : cause.validation.message
      }
      if (cause.message.trim()) return cause.message
    }
    if (cause instanceof Error && cause.message.trim()) return cause.message
    if (typeof cause === 'string' && cause.trim()) return cause
    if (cause && typeof cause === 'object') {
      const record = cause as Record<string, unknown>
      for (const key of ['message', 'error', 'detail'] as const) {
        const value = record[key]
        if (typeof value === 'string' && value.trim()) return value
      }
    }
    return fallback
  }

  const activateManagedLocal = async () => {
    setBusyAction('local')
    setError(null)
    setLocalPhase('checking')
    try {
      await ensureManagedLocalReady(setManaged, setLocalPhase)
      const activated = await setupApi.activateManagedLocal()
      if (!activated.core_ready) throw new Error(activated.message)
      setLocalSetupState(activated.state)
      setSetupState(activated.state)
      setManaged(await setupApi.getManagedLocalStatus().catch(() => null))
    } catch (e) {
      if (e instanceof ManagedLocalDownloadPausedError) {
        setManaged(await setupApi.getManagedLocalStatus().catch(() => null))
        setError(null)
      } else {
        captureConfigurationError(e, 'Could not activate the on-device model.')
      }
    } finally {
      setBusyAction(null)
      setLocalPhase(null)
    }
  }

  const cancelManagedDownload = async () => {
    setBusyAction('cancel')
    setError(null)
    try {
      const status = await setupApi.cancelManagedLocal()
      setManaged(status)
      setLocalPhase(null)
    } catch (e) {
      captureConfigurationError(e, 'Could not pause the download.')
    } finally {
      setBusyAction(null)
    }
  }

  const removeManagedLocal = async () => {
    if (!window.confirm('Remove the on-device model download from this Mac?')) return
    setBusyAction('remove')
    setError(null)
    try {
      if (managed?.active) {
        throw new Error('Switch to a cloud or custom model before removing the on-device download.')
      }
      if (isDesktopApp()) {
        await setManagedLocalLlmEnabled(true)
      }
      const status = await setupApi.removeManagedLocal()
      setManaged(status)
      if (isDesktopApp() && activePresetId !== 'local') {
        await setManagedLocalLlmEnabled(false)
      }
    } catch (e) {
      captureConfigurationError(e, 'Could not remove the on-device model.')
    } finally {
      setBusyAction(null)
    }
  }

  const captureConfigurationError = (
    cause: unknown,
    fallback: string,
    body?: ConfigureLlmRequest,
  ) => {
    if (cause instanceof SetupApiError && cause.validation) {
      setValidation(cause.validation)
      setFailedConfig(body ?? null)
      setError(formatCaughtError(cause, fallback))
      return
    }
    setValidation(null)
    setFailedConfig(null)
    setError(formatCaughtError(cause, fallback))
  }

  const activatePreset = async (presetId: ModelPresetId) => {
    if (presetId === activePresetId) {
      setKeyPreset(null)
      setError(null)
      return
    }

    const preset = MODEL_PRESETS.find((item) => item.id === presetId)
    if (!preset) return

    if (presetId === 'local') {
      setKeyPreset(null)
      setAdvancedOpen(false)
      await activateManagedLocal()
      return
    }

    const provider = presetProvider(presetId, providers)
    if (!provider?.key_stored) {
      setKeyPreset(presetId)
      setApiKey('')
      setAdvancedOpen(false)
      setError(null)
      return
    }

    setBusyAction(presetId)
    setError(null)
    setKeyPreset(null)
    setAdvancedOpen(false)
    const body = {
        provider: preset.provider,
        model: presetModel(presetId, providers),
      }
    try {
      await applyConfig(body)
    } catch (e) {
      captureConfigurationError(e, 'Could not switch model.', body)
    } finally {
      setBusyAction(null)
    }
  }

  const saveKeyAndActivate = async () => {
    if (!keyPreset) return
    const preset = MODEL_PRESETS.find((item) => item.id === keyPreset)
    if (!preset) return

    setBusyAction(keyPreset)
    setError(null)
    const body = {
        provider: preset.provider,
        api_key: apiKey,
        model: presetModel(keyPreset, providers),
      }
    try {
      await applyConfig(body)
      const refreshed = await setupApi.listProviders()
      setProviders(refreshed)
      setApiKey('')
      setKeyPreset(null)
    } catch (e) {
      captureConfigurationError(e, 'Could not save API key.', body)
    } finally {
      setBusyAction(null)
    }
  }

  const applyAdvanced = async () => {
    setBusyAction('advanced')
    setError(null)
    let attemptedConfig: ConfigureLlmRequest | undefined
    try {
      const providerMeta = providers.find((provider) => provider.id === advancedProvider)
      const isLocal = isByoLocalProvider(advancedProvider)
      if (isLocal) {
        if (!selectedLocalRuntime?.reachable || !selectedLocalModel) {
          setError('Pick a reachable server and model under Advanced.')
          return
        }
        attemptedConfig = {
          provider: selectedLocalRuntime.runtime,
          model: selectedLocalModel,
          base_url: selectedLocalRuntime.base_url,
        }
        await applyConfig(attemptedConfig)
      } else if (!providerMeta?.key_stored && !apiKey.trim()) {
        setError('Add an API key for this provider before applying.')
        return
      } else {
        attemptedConfig = {
          provider: advancedProvider,
          model: advancedModel || providerMeta?.default_model,
          base_url: advancedBaseUrl || undefined,
          api_key: apiKey.trim() || undefined,
        }
        await applyConfig(attemptedConfig)
        const refreshed = await setupApi.listProviders()
        setProviders(refreshed)
        setApiKey('')
      }
    } catch (e) {
      captureConfigurationError(e, 'Could not apply model settings.', attemptedConfig)
    } finally {
      setBusyAction(null)
    }
  }

  const scanLocal = async (): Promise<LocalLlmRuntime | null> => {
    setScanHint(null)
    try {
      const runtimes = await setupApi.discoverLocalLlms()
      setLocalRuntimes(runtimes)
      const firstReachable = runtimes.find((runtime) => runtime.reachable) ?? null
      setSelectedLocalRuntime(firstReachable)
      setSelectedLocalModel(firstReachable?.models[0] ?? '')
      if (firstReachable) {
        setAdvancedProvider(firstReachable.runtime)
      } else {
        setScanHint(
          'No Ollama, LM Studio, or llama.cpp server detected. Start one, then scan again — or use On this Mac above.',
        )
      }
      return firstReachable
    } catch (e) {
      setScanHint(e instanceof Error ? e.message : 'Could not scan for local servers.')
      return null
    }
  }

  const useRecommendedModel = async () => {
    if (!failedConfig || !validation?.recommended_model) return
    const body = { ...failedConfig, model: validation.recommended_model }
    setBusyAction('advanced')
    setError(null)
    try {
      await applyConfig(body)
      setValidation(null)
      setFailedConfig(null)
      setApiKey('')
      setKeyPreset(null)
    } catch (e) {
      captureConfigurationError(e, 'Could not activate the recommended model.', body)
    } finally {
      setBusyAction(null)
    }
  }

  if (!active) return null

  if (loading && !setupState) {
    return (
      <PanelSection className="p-5" aria-busy="true">
        <div className="flex items-center gap-3 text-foreground-muted">
          <SpinnerIcon size={16} className="animate-spin" />
          <p className="type-body">Loading model settings…</p>
        </div>
        <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2" aria-hidden>
          {Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="min-h-24 animate-pulse rounded-control bg-surface/20" />
          ))}
        </div>
      </PanelSection>
    )
  }

  const llm = setupState?.llm
  const statusTone = llm?.configured ? 'success' : 'error'

  return (
    <PanelSection className="p-5">
      <div className="flex items-start gap-3">
        <StatusDot status={statusTone} size="md" className="mt-1.5" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="type-heading text-foreground">Current model</h3>
            {llm?.configured && (
              <StatusPill tone="success">Active</StatusPill>
            )}
          </div>

          {llm && (
            <>
              <p className="mt-1 text-sm font-body leading-relaxed text-foreground">
                {isManagedActive ? `On this Mac · ${activeSummary}` : activeSummary}
              </p>
              <p className="mt-1 text-sm font-body leading-relaxed text-foreground-muted">{activeSubtitle}</p>
              <p className="mt-1 text-xs font-body leading-relaxed text-foreground-subtle">
                Changes apply on your next message.
              </p>
            </>
          )}

          {error && (
            <div className="mt-2 space-y-2">
              <p className="flex items-start gap-1.5 text-sm font-body text-status-danger">
                <WarningIcon size={14} className="mt-0.5 shrink-0" />
                <span>{error}</span>
              </p>
              {validation?.code === 'model_unavailable'
                && validation.recommended_model
                && failedConfig
                && validation.recommended_model !== failedConfig.model && (
                <Button
                  color="neutral"
                  size="xs"
                  disabled={busyAction !== null}
                  onClick={() => void useRecommendedModel()}
                >
                  Use {validation.recommended_model}
                </Button>
              )}
            </div>
          )}

          <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2">
            {MODEL_PRESETS.map((preset) => {
              const provider = presetProvider(preset.id, providers)
              const selected = activePresetId === preset.id
              const needsKey = preset.id !== 'local' && !provider?.key_stored
              const localUnsupported = preset.id === 'local' && managed?.supported === false
              const disabled = busyAction !== null || localUnsupported
              const providerLabel = preset.id === 'local'
                ? (managed?.model_label || 'Private baseline')
                : formatProviderLabel(preset.provider)
              const localSubtitle = preset.id === 'local' && managed && !managed.model_installed && managed.supported
                ? `About ${(managed.approx_download_bytes / 1024 ** 3).toFixed(0)} GB · no API key`
                : preset.subtitle
              const localBusy = preset.id === 'local' && busyAction === 'local'
              const stateLabel = selected
                ? 'Active'
                : needsKey
                  ? 'Needs key'
                  : preset.id === 'local'
                    ? localBusy
                      ? localPhase === 'downloading'
                        ? 'Downloading'
                        : localPhase === 'starting'
                          ? 'Starting'
                          : 'Working'
                      : managed?.status === 'downloading'
                        ? 'Downloading'
                        : managed?.model_installed
                          ? 'Ready'
                          : managed?.supported === false
                            ? 'Unavailable'
                            : 'Download'
                    : 'Ready'
              return (
                <button
                  key={preset.id}
                  disabled={disabled}
                  type="button"
                  onClick={() => void activatePreset(preset.id)}
                  className={cn(
                    'min-h-24 rounded-control border px-4 py-3 text-left transition-colors duration-200',
                    'focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/50',
                    disabled && 'cursor-not-allowed opacity-50',
                    selected
                      ? 'border-brand/60 bg-brand/10'
                      : 'border-outline/20 bg-canvas-sunken/20 hover:border-brand/35 hover:bg-brand/5',
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="type-label text-foreground">
                      {preset.label}
                    </span>
                    <span
                      className={cn(
                        'type-meta',
                        selected ? 'text-status-success' : needsKey || localUnsupported ? 'text-foreground-disabled' : 'text-foreground-subtle',
                      )}
                    >
                      {busyAction === preset.id ? <SpinnerIcon size={12} className="animate-spin" /> : stateLabel}
                    </span>
                  </div>
                  <p className="mt-2 text-sm font-body leading-relaxed text-foreground-muted">{providerLabel}</p>
                  <p className="mt-1 text-xs font-body leading-relaxed text-foreground-subtle">{localSubtitle}</p>
                </button>
              )
            })}
          </div>

          {(localPhase || (managed && (managed.model_installed || managed.status === 'downloading' || (managed.detail || '').toLowerCase().includes('paused')))) && (
            <div className="mt-4 space-y-2 rounded-control border border-outline/20 bg-canvas-sunken/20 px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <p className="min-w-0 flex-1 text-xs font-body leading-relaxed text-foreground-muted">
                  {localPhase === 'starting'
                    ? 'Starting the on-device runtime…'
                    : localPhase === 'checking'
                      ? 'Checking this Mac…'
                      : managed?.status === 'downloading' || localPhase === 'downloading'
                        ? `Downloading ${managed?.model_label || 'on-device model'}…`
                        : managed?.active
                          ? `${managed.model_label} is active on this Mac.`
                          : managed?.model_installed
                            ? `${managed.model_label} is still installed. Remove it to free disk space.`
                            : (managed?.detail || '').toLowerCase().includes('paused')
                              ? `${managed?.model_label || 'On-device model'} download paused. Choose On this Mac to resume.`
                              : 'Preparing on-device model…'}
                </p>
                {managed && (managed.status === 'downloading' || localPhase === 'downloading') && (
                  <Button
                    color="neutral"
                    size="xs"
                    variant="ghost"
                    disabled={busyAction === 'cancel'}
                    onClick={() => void cancelManagedDownload()}
                  >
                    {busyAction === 'cancel' ? <SpinnerIcon size={12} className="animate-spin" /> : 'Pause download'}
                  </Button>
                )}
                {managed && !managed.active && managed.model_installed && !localPhase && managed.status !== 'downloading' && (
                  <Button
                    color="neutral"
                    size="xs"
                    variant="ghost"
                    disabled={busyAction !== null}
                    onClick={() => void removeManagedLocal()}
                  >
                    {busyAction === 'remove' ? <SpinnerIcon size={12} className="animate-spin" /> : 'Remove download'}
                  </Button>
                )}
              </div>
              {(managed?.status === 'downloading' || localPhase === 'downloading') && (
                <div className="space-y-1">
                  <div className="h-1.5 overflow-hidden rounded-full bg-surface/40">
                    <div
                      className={cn(
                        'h-full rounded-full bg-brand transition-all duration-300',
                        managed && managed.total_bytes > 0 && managed.completed_bytes === 0 && 'animate-pulse',
                      )}
                      style={{
                        width: `${
                          managed && managed.total_bytes > 0
                            ? Math.max(
                                managed.completed_bytes > 0 ? 1 : 4,
                                downloadProgressPct(managed.completed_bytes, managed.total_bytes),
                              )
                            : 4
                        }%`,
                      }}
                    />
                  </div>
                  <p className="text-[11px] font-mono text-foreground-subtle">
                    {managed?.detail || 'Preparing download…'}
                    {managed && managed.total_bytes > 0
                      ? ` · ${formatDownloadBytes(managed.completed_bytes)} / ${formatDownloadBytes(managed.total_bytes)}`
                      : managed?.approx_download_bytes
                        ? ` · about ${formatDownloadBytes(managed.approx_download_bytes)}`
                        : ''}
                    {managed && managed.total_bytes > 0
                      ? ` · ${downloadProgressPct(managed.completed_bytes, managed.total_bytes)}%`
                      : ''}
                  </p>
                </div>
              )}
            </div>
          )}

          {keyPreset && (
            <div className="mt-4 space-y-3 rounded-control border border-brand/25 bg-brand/[0.04] p-4">
              <div>
                <p className="font-mono text-xs uppercase tracking-[0.15em] text-foreground">
                  Add key to activate {MODEL_PRESETS.find((preset) => preset.id === keyPreset)?.label}
                </p>
                <p className="mt-1 text-sm font-body leading-relaxed text-foreground-subtle">
                  Encrypted on this Mac. The switch applies after validation.
                </p>
                {presetProvider(keyPreset, providers)?.signup_url && (
                  <a
                    href={presetProvider(keyPreset, providers)?.signup_url}
                    target="_blank"
                    rel="noreferrer"
                    className="mt-1 inline-block text-xs text-brand hover:underline"
                  >
                    Get an API key
                  </a>
                )}
              </div>
              <FieldControl label={`API key for ${formatProviderLabel(MODEL_PRESETS.find((preset) => preset.id === keyPreset)?.provider)}`} htmlFor="model-switcher-api-key">
                <Input
                  id="model-switcher-api-key"
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  className="font-mono"
                />
              </FieldControl>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  color="brand"
                  size="sm"
                  disabled={busyAction !== null || !apiKey.trim()}
                  onClick={() => void saveKeyAndActivate()}
                >
                  {busyAction === keyPreset ? <SpinnerIcon size={14} className="animate-spin" /> : 'Connect'}
                </Button>
                <Button color="neutral" size="sm" variant="ghost" disabled={busyAction !== null} onClick={() => setKeyPreset(null)}>
                  Cancel
                </Button>
              </div>
            </div>
          )}

          <button
            type="button"
            className="mt-4 flex min-h-10 items-center gap-1.5 type-label-small text-foreground-subtle transition-colors duration-feedback hover:text-foreground"
            onClick={() => {
              setKeyPreset(null)
              setScanHint(null)
              setAdvancedOpen((open) => !open)
            }}
          >
            {advancedOpen ? <CaretDownIcon size={12} /> : <CaretRightIcon size={12} />}
            Advanced
          </button>

          {advancedOpen && (
            <div className="mt-3 space-y-3 rounded-control border border-outline/20 bg-canvas-sunken/30 p-4">
              <p className="text-xs font-body leading-relaxed text-foreground-subtle">
                Custom cloud provider, or connect a server you already run (Ollama, LM Studio, llama.cpp).
                For a private JARV1S baseline, use On this Mac above.
              </p>
              <FieldControl label="Provider">
                <Select
                  aria-label="Provider"
                  value={advancedProvider}
                  onChange={(next) => {
                    setAdvancedProvider(next)
                    setScanHint(null)
                    const providerMeta = providers.find((provider) => provider.id === next)
                    setAdvancedModel(providerMeta?.default_model ?? '')
                    if (isByoLocalProvider(next) && localRuntimes.length === 0) {
                      void scanLocal()
                    }
                  }}
                  options={[
                    ...providers
                      .filter((provider) => !isByoLocalProvider(provider.id))
                      .map((provider) => ({
                        value: provider.id,
                        label: provider.label,
                        group: 'Cloud',
                      })),
                    ...['ollama', 'lmstudio', 'llamacpp'].map((runtimeId) => {
                      const discovered = localRuntimes.find((runtime) => runtime.runtime === runtimeId)
                      const fallback = providers.find((provider) => provider.id === runtimeId)
                      return {
                        value: runtimeId,
                        label: discovered?.label || fallback?.label || runtimeId,
                        group: 'Your own local server',
                      }
                    }),
                  ]}
                />
              </FieldControl>

              {isByoLocalProvider(advancedProvider) ? (
                <div className="space-y-3">
                  <div className="flex items-center gap-2">
                    <Button color="neutral" size="xs" variant="ghost" onClick={() => void scanLocal()}>
                      Scan this Mac
                    </Button>
                  </div>
                  {scanHint && (
                    <p className="text-xs font-body leading-relaxed text-foreground-subtle">{scanHint}</p>
                  )}
                  {localRuntimes.map((runtime) => (
                    <button
                      key={runtime.runtime}
                      type="button"
                      onClick={() => {
                        setSelectedLocalRuntime(runtime)
                        setSelectedLocalModel(runtime.models[0] ?? '')
                        setAdvancedProvider(runtime.runtime)
                        setScanHint(null)
                      }}
                      className={cn(
                        'w-full rounded-control border px-3 py-2 text-left transition-colors',
                        selectedLocalRuntime?.runtime === runtime.runtime
                          ? 'border-brand/50 bg-brand/10'
                          : 'border-outline/20 hover:border-outline/40',
                      )}
                    >
                      <p className="text-xs text-foreground">{runtime.label}</p>
                      <p className="text-meta text-foreground-subtle mt-0.5">
                        {runtime.reachable
                          ? `${runtime.models.length} model(s) available`
                          : runtime.detail ?? 'Not reachable'}
                      </p>
                    </button>
                  ))}
                  {selectedLocalRuntime && selectedLocalRuntime.models.length > 0 && (
                    <FieldControl label="Model">
                      <Select
                        aria-label="Model"
                        value={selectedLocalModel}
                        onChange={setSelectedLocalModel}
                        options={selectedLocalRuntime.models.map((model) => ({ value: model, label: model }))}
                      />
                    </FieldControl>
                  )}
                </div>
              ) : (
                <>
                  <FieldControl label="Model" htmlFor="model-switcher-advanced-model">
                    <Input
                      id="model-switcher-advanced-model"
                      type="text"
                      value={advancedModel}
                      onChange={(e) => setAdvancedModel(e.target.value)}
                      placeholder={providers.find((provider) => provider.id === advancedProvider)?.default_model}
                      className="font-mono"
                    />
                  </FieldControl>
                  {!providers.find((provider) => provider.id === advancedProvider)?.key_stored && (
                    <FieldControl label="API key" htmlFor="model-switcher-advanced-key">
                      <Input
                        id="model-switcher-advanced-key"
                        type="password"
                        value={apiKey}
                        onChange={(e) => setApiKey(e.target.value)}
                        autoComplete="off"
                        className="font-mono"
                      />
                    </FieldControl>
                  )}
                  <FieldControl label="Base URL" htmlFor="model-switcher-advanced-url">
                    <Input
                      id="model-switcher-advanced-url"
                      type="text"
                      value={advancedBaseUrl}
                      onChange={(e) => setAdvancedBaseUrl(e.target.value)}
                      placeholder="Optional — provider default"
                      className="font-mono"
                    />
                  </FieldControl>
                </>
              )}

              <Button color="brand" size="sm" disabled={busyAction !== null} onClick={() => void applyAdvanced()}>
                {busyAction === 'advanced' ? <SpinnerIcon size={14} className="animate-spin" /> : 'Connect'}
              </Button>
            </div>
          )}
        </div>
      </div>
    </PanelSection>
  )
}
