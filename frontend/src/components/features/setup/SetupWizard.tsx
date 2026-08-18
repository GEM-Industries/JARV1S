import React, { useCallback, useEffect, useState } from 'react'
import {
  ArrowRightIcon,
  BrainIcon,
  CheckCircleIcon,
  CloudIcon,
  DesktopIcon,
  KeyIcon,
  SpinnerIcon,
  WarningCircleIcon,
} from '@phosphor-icons/react'
import {
  setupApi,
  SetupApiError,
  type LocalLlmRuntime,
  type LlmProviderOption,
  type ManagedLlmStatus,
  type SetupState,
  type ValidationResult,
} from '../../../client/setupApi'
import { voiceApi } from '../../../client/voiceApi'
import { CLOUD_MODEL_PRESETS } from '../../../constants/modelPresets'
import { isDesktopApp } from '../../../runtime/clientSurface'
import { setManagedLocalLlmEnabled } from '../../../runtime/desktopBridge'
import { ensureManagedLocalReady, ManagedLocalDownloadPausedError } from '../../../client/managedLocalLlm'
import { useJarvisStore } from '../../../store/useJarvisStore'
import { cn } from '../../../utils/cn'
import { Button } from '../../ui/Button'
import { FieldControl, Input } from '../../ui/FieldControl'
import { Hologram } from '../../ui/Hologram'
import { PanelSection } from '../../ui/PanelSection'
import { Select } from '../../ui/Select'
import { TextLink } from '../../ui/TextLink'
import { OwnerVoiceEnrollment } from '../voice/OwnerVoiceEnrollment'

type WizardStep =
  | 'welcome'
  | 'brain_choice'
  | 'local_install'
  | 'local_advanced'
  | 'provider'
  | 'apikey'
  | 'voice_profile'
  | 'ready'

const STARTER_PROMPT = 'What can you help me with?'

const STEP_META: Record<WizardStep, string> = {
  welcome: 'First-time setup',
  brain_choice: 'Step 1 of 2',
  local_install: 'Step 2 of 2',
  local_advanced: 'Step 2 of 2',
  provider: 'Step 2 of 3',
  apikey: 'Step 3 of 3',
  voice_profile: 'Recommended · optional',
  ready: 'Setup complete',
}

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 B'
  if (bytes < 1024 ** 3) {
    const mb = bytes / 1024 ** 2
    return `${mb >= 100 ? mb.toFixed(0) : mb.toFixed(1)} MB`
  }
  const gb = bytes / 1024 ** 3
  return `${gb >= 10 ? gb.toFixed(0) : gb.toFixed(1)} GB`
}

const localRuntimeGuidance = (runtime: LocalLlmRuntime): string | null => {
  if (runtime.models.length > 0) return null
  if (runtime.runtime === 'ollama') {
    return 'Ollama is running but no models are installed. Pull a model, then scan again.'
  }
  if (runtime.runtime === 'lmstudio') {
    return 'LM Studio is running but no model is loaded. Load a model, then scan again.'
  }
  if (runtime.runtime === 'llamacpp') {
    return 'llama.cpp is running but did not list a model. Start llama-server with a GGUF, then scan again.'
  }
  return runtime.detail ?? 'Runtime is reachable but no models were listed.'
}

interface SetupWizardProps {
  initialState: SetupState
  onComplete: () => void
}

interface SetupChoiceButtonProps {
  icon: React.ReactNode
  title: string
  description: string
  badge?: string
  disabled?: boolean
  onClick: () => void
}

const SetupChoiceButton: React.FC<SetupChoiceButtonProps> = ({
  icon,
  title,
  description,
  badge,
  disabled,
  onClick,
}) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    className="group ui-surface-selectable flex min-h-24 w-full items-center gap-4 rounded-panel bg-surface/15 px-4 py-4 text-left transition-colors duration-feedback hover:bg-surface/25 focus:outline-none disabled:cursor-not-allowed disabled:opacity-50 sm:px-5"
  >
    <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-control border border-outline/40 bg-surface/30 text-brand transition-colors duration-feedback group-hover:border-brand/40 group-hover:bg-brand/10">
      {icon}
    </span>
    <span className="min-w-0 flex-1">
      <span className="flex flex-wrap items-center gap-2">
        <span className="type-heading text-foreground">{title}</span>
        {badge && (
          <span className="rounded-full border border-brand/35 bg-brand/10 px-2 py-1 type-fui text-brand">
            {badge}
          </span>
        )}
      </span>
      <span className="mt-1 block type-body text-foreground-muted">{description}</span>
    </span>
    <ArrowRightIcon
      className="shrink-0 text-foreground-subtle transition-colors duration-feedback group-hover:text-brand"
      size={18}
    />
  </button>
)

const SetupActions: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div className="flex flex-wrap items-center gap-3 pt-2">{children}</div>
)

export const SetupWizard: React.FC<SetupWizardProps> = ({ initialState, onComplete }) => {
  const setSetupState = useJarvisStore((s) => s.setSetupState)

  const [currentState, setCurrentState] = useState(initialState)
  const [step, setStep] = useState<WizardStep>('welcome')
  const [providers, setProviders] = useState<LlmProviderOption[]>([])
  const [managed, setManaged] = useState<ManagedLlmStatus | null>(null)
  const [localRuntimes, setLocalRuntimes] = useState<LocalLlmRuntime[]>([])
  const [selectedLocalRuntime, setSelectedLocalRuntime] = useState<LocalLlmRuntime | null>(null)
  const [selectedLocalModel, setSelectedLocalModel] = useState('')
  const [selectedProvider, setSelectedProvider] = useState<string>(
    initialState.llm.provider || 'google-ai-studio',
  )
  const [apiKey, setApiKey] = useState('')
  const [loading, setLoading] = useState(false)
  const [pausingDownload, setPausingDownload] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const [servicesBlocked, setServicesBlocked] = useState(
    initialState.services.some((s) => s.status === 'down'),
  )

  useEffect(() => {
    setCurrentState(initialState)
    setServicesBlocked(initialState.services.some((s) => s.status === 'down'))
  }, [initialState])

  useEffect(() => {
    setupApi.listProviders().then(setProviders).catch(() => {
      setError('Could not load provider list.')
    })
  }, [])

  useEffect(() => {
    const curatedProviders = CLOUD_MODEL_PRESETS
      .map((preset) => providers.find((provider) => provider.id === preset.provider))
      .filter((provider): provider is LlmProviderOption => provider !== undefined)
    if (curatedProviders.length && !curatedProviders.some((p) => p.id === selectedProvider)) {
      setSelectedProvider(curatedProviders[0].id)
    }
  }, [providers, selectedProvider])

  const selectedPreset = providers.find((p) => p.id === selectedProvider)

  const refreshState = useCallback(async () => {
    const state = await setupApi.getState()
    setCurrentState(state)
    setSetupState(state)
    setServicesBlocked(state.services.some((s) => s.status === 'down'))
    return state
  }, [setSetupState])

  const captureConfigurationError = (cause: unknown, fallback: string) => {
    if (cause instanceof SetupApiError && cause.validation) {
      setValidation(cause.validation)
      setError(cause.validation.message)
      return
    }
    setValidation(null)
    if (cause instanceof Error && cause.message.trim()) {
      setError(cause.message)
      return
    }
    if (typeof cause === 'string' && cause.trim()) {
      setError(cause)
      return
    }
    setError(fallback)
  }

  useEffect(() => {
    if (step !== 'ready') return
    void voiceApi.prepareInput()
  }, [step])

  const offerVoiceProfile = async () => {
    await refreshState()
    try {
      const status = await voiceApi.getSpeakerProfile()
      if (status.status === 'enrolled') {
        setStep('ready')
        return
      }
    } catch {
      // Offer enrollment even if status cannot be loaded; the step has its own retry.
    }
    setStep('voice_profile')
  }

  const beginManagedLocal = async () => {
    setLoading(true)
    setError(null)
    setValidation(null)
    setStep('local_install')
    try {
      await ensureManagedLocalReady(setManaged)
      const activated = await setupApi.activateManagedLocal()
      if (!activated.core_ready) throw new Error(activated.message)
      setSetupState(activated.state)
      await offerVoiceProfile()
    } catch (e) {
      if (e instanceof ManagedLocalDownloadPausedError) {
        setManaged(await setupApi.getManagedLocalStatus().catch(() => null))
        setError(null)
        setStep('brain_choice')
      } else {
        captureConfigurationError(e, 'Could not set up the on-device model.')
      }
    } finally {
      setLoading(false)
    }
  }

  const cancelManagedDownload = async () => {
    setPausingDownload(true)
    setError(null)
    try {
      const status = await setupApi.cancelManagedLocal()
      setManaged(status)
      setStep('brain_choice')
    } catch (e) {
      captureConfigurationError(e, 'Could not pause the download.')
    } finally {
      setPausingDownload(false)
    }
  }

  const loadLocalRuntimes = async () => {
    setLoading(true)
    setError(null)
    try {
      const runtimes = await setupApi.discoverLocalLlms()
      setLocalRuntimes(runtimes)
      const firstReachable = runtimes.find((runtime) => runtime.reachable)
      if (firstReachable) {
        setSelectedLocalRuntime(firstReachable)
        setSelectedLocalModel(firstReachable.models[0] ?? '')
      } else {
        setSelectedLocalRuntime(null)
        setSelectedLocalModel('')
      }
      setStep('local_advanced')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not scan for local runtimes.')
    } finally {
      setLoading(false)
    }
  }

  const handleConnectExternalLocal = async () => {
    if (!selectedLocalRuntime || !selectedLocalModel) return
    setLoading(true)
    setError(null)
    setValidation(null)
    try {
      const result = await setupApi.activateLlm({
        provider: selectedLocalRuntime.runtime,
        api_key: '',
        model: selectedLocalModel,
        base_url: selectedLocalRuntime.base_url,
      })
      if (!result.core_ready) throw new Error(result.message)
      if (isDesktopApp()) {
        await setManagedLocalLlmEnabled(false)
      }
      setSetupState(result.state)
      await offerVoiceProfile()
    } catch (e) {
      captureConfigurationError(e, 'Could not connect the local model.')
    } finally {
      setLoading(false)
    }
  }

  const handleConnectCloud = async (modelOverride?: string) => {
    setLoading(true)
    setError(null)
    setValidation(null)
    try {
      const result = await setupApi.activateLlm({
        provider: selectedProvider,
        api_key: apiKey,
        model: modelOverride ?? selectedPreset?.recommended_model,
      })
      if (!result.core_ready) throw new Error(result.message)
      if (isDesktopApp()) {
        await setManagedLocalLlmEnabled(false)
      }
      setSetupState(result.state)
      await offerVoiceProfile()
    } catch (e) {
      captureConfigurationError(e, 'Could not connect the provider.')
    } finally {
      setLoading(false)
    }
  }

  const progressPct = managed && managed.total_bytes > 0
    ? Math.min(99, Math.round((managed.completed_bytes / managed.total_bytes) * 100))
    : 0

  if (servicesBlocked) {
    return (
      <main className="stage-background h-dvh overflow-y-auto">
        <div className="flex min-h-full items-center justify-center p-6">
          <Hologram
            variant="base"
            color="warning"
            className="w-full max-w-lg bg-canvas-sunken/90 p-6 shadow-2xl shadow-black/20 sm:p-8"
          >
            <div className="flex flex-col gap-4 text-center">
              <WarningCircleIcon className="mx-auto text-status-warning" size={40} />
              <h1 className="type-section text-foreground">Waiting for JARV1S</h1>
              <p className="type-body text-foreground-muted">
                {currentState.blocking_reason ?? 'Local data is still starting.'}
              </p>
              <p className="type-meta text-foreground-subtle">
                {currentState.next_action ?? 'Give it a moment, then check again.'}
              </p>
              <Button color="brand" onClick={() => refreshState().catch(console.error)}>
                Check again
              </Button>
            </div>
          </Hologram>
        </div>
      </main>
    )
  }

  return (
    <main className="stage-background h-dvh overflow-y-auto">
      <div className="flex min-h-full flex-col px-4 py-8 sm:px-6 md:py-12">
        <Hologram
          variant="base"
          className="mx-auto my-auto w-full max-w-2xl bg-canvas-sunken/90 p-6 shadow-2xl shadow-black/20 sm:p-8 md:p-10"
        >
          <div className="flex flex-col gap-8">
            <header className="space-y-3 text-center">
              <p className="type-fui text-brand">{STEP_META[step]}</p>
              <h1 className="type-title text-foreground">
              {step === 'welcome' && 'Set up JARV1S'}
              {step === 'brain_choice' && 'Choose how JARV1S replies'}
              {step === 'local_install' && 'Install on this Mac'}
              {step === 'local_advanced' && 'Use your own local server'}
              {step === 'provider' && 'Choose your cloud provider'}
              {step === 'apikey' && 'Add your API key'}
              {step === 'voice_profile' && 'Teach JARV1S your voice'}
              {step === 'ready' && 'You’re ready'}
              </h1>
              <p className="mx-auto max-w-lg type-body text-foreground-muted">
              {step === 'welcome' &&
                'Connect an AI model so JARV1S can reply. You can change this later in Settings.'}
              {step === 'brain_choice' &&
                'Private on this Mac by default, or use a cloud provider with an API key.'}
              {step === 'local_install' &&
                'JARV1S will download one on-device model. You can remove it anytime from Settings.'}
              {step === 'local_advanced' &&
                'Connect Ollama, LM Studio, or llama.cpp if you already run one. JARV1S will not manage that install.'}
              {step === 'provider' && 'Pick a provider. The default model is selected for you.'}
              {step === 'apikey' && 'Your key is stored securely on this Mac.'}
              {step === 'voice_profile' &&
                'So wake and interrupt prefer your voice. Skip anytime — you can finish later in Settings → Voice & Audio.'}
              {step === 'ready' &&
                'Typing works immediately. macOS may ask for Speech Recognition so you can talk on this Mac.'}
              </p>
            </header>

          {step === 'welcome' && (
            <div className="space-y-4">
              <PanelSection className="flex items-start gap-4 p-5">
                <BrainIcon className="text-brand shrink-0 mt-0.5" size={24} />
                <div>
                  <p className="type-label text-foreground">One required choice</p>
                  <p className="mt-1 type-body text-foreground-muted">
                    Choose how JARV1S replies. You can type or talk after this — apps stay optional.
                  </p>
                </div>
              </PanelSection>
              <Button
                className="w-full"
                color="brand"
                icon={<ArrowRightIcon size={16} />}
                iconPosition="end"
                onClick={() => setStep('brain_choice')}
              >
                Start setup
              </Button>
            </div>
          )}

          {step === 'brain_choice' && (
            <div className="space-y-4">
              <div className="space-y-3">
                <SetupChoiceButton
                  icon={<DesktopIcon size={22} />}
                  title="Run on this Mac"
                  badge={isDesktopApp() ? 'Recommended' : undefined}
                  description="JARV1S downloads one private model (~9 GB). No API key. Fast local replies on Apple Silicon with 16 GB+ memory."
                  disabled={loading}
                  onClick={() => void beginManagedLocal()}
                />
                <SetupChoiceButton
                  icon={<CloudIcon size={22} />}
                  title="Use a cloud provider"
                  description="Paste one API key and start chatting. No large download."
                  onClick={() => setStep('provider')}
                />
              </div>
              <TextLink
                onClick={() => void loadLocalRuntimes()}
              >
                Use my own Ollama, LM Studio, or llama.cpp instead
              </TextLink>
              <SetupActions>
                <Button
                  variant="ghost"
                  color="neutral"
                  size="md"
                  className="min-w-28"
                  onClick={() => setStep('welcome')}
                >
                  Back
                </Button>
              </SetupActions>
            </div>
          )}

          {step === 'local_install' && (
            <div className="space-y-5">
              {managed && !managed.supported && (
                <PanelSection className="border-status-warning/35 bg-status-warning/10 p-4 type-body text-foreground-muted">
                  {managed.detail}
                  {' '}Use a cloud provider or your own local server instead.
                </PanelSection>
              )}

              {!managed && loading && (
                <PanelSection className="flex items-center gap-3 p-5" aria-live="polite">
                  <SpinnerIcon className="shrink-0 animate-spin text-brand" size={20} aria-hidden />
                  <div>
                    <p className="type-label text-foreground">Preparing the on-device model</p>
                    <p className="mt-1 type-meta text-foreground-muted">
                      Checking compatibility and download requirements…
                    </p>
                  </div>
                </PanelSection>
              )}

              {managed?.supported && (
                <PanelSection className="space-y-3 p-5">
                  <p className="type-label text-foreground">
                    {managed.model_label}
                  </p>
                  <p className="type-body text-foreground-muted">
                    About {formatBytes(managed.approx_download_bytes)} download.
                    Needs {formatBytes(managed.min_memory_bytes)} memory and{' '}
                    {formatBytes(managed.min_disk_bytes)} free disk.
                  </p>
                  {managed.model_license_url && (
                    <TextLink
                      href={managed.model_license_url}
                      external
                      className="type-meta"
                    >
                      Model license terms
                    </TextLink>
                  )}
                  {(managed.status === 'downloading' || loading) && (
                    <div className="space-y-2 pt-2">
                      <div
                        className="h-2 overflow-hidden rounded-full bg-surface/40"
                        role="progressbar"
                        aria-label="Model download"
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={managed.total_bytes > 0 ? progressPct : undefined}
                      >
                        <div
                          className={cn(
                            'h-full rounded-full bg-brand transition-[width] duration-transition',
                            managed.total_bytes <= 0 && 'animate-pulse',
                          )}
                          style={{ width: managed.total_bytes > 0 ? `${progressPct}%` : '33%' }}
                        />
                      </div>
                      <p className="type-meta text-foreground-subtle" aria-live="polite">
                        {managed.detail || 'Preparing download…'}
                        {managed.total_bytes > 0
                          ? ` · ${formatBytes(managed.completed_bytes)} / ${formatBytes(managed.total_bytes)}`
                          : ''}
                      </p>
                    </div>
                  )}
                </PanelSection>
              )}

              <SetupActions>
                {managed?.status === 'downloading' ? (
                  <Button
                    className="flex-1"
                    size="md"
                    color="neutral"
                    variant="ghost"
                    disabled={pausingDownload}
                    onClick={() => void cancelManagedDownload()}
                  >
                    {pausingDownload && <SpinnerIcon className="animate-spin" size={18} />}
                    {pausingDownload ? 'Pausing…' : 'Pause download'}
                  </Button>
                ) : (
                  <Button
                    variant="ghost"
                    color="neutral"
                    size="md"
                    className="min-w-24"
                    onClick={() => setStep('brain_choice')}
                  >
                    Back
                  </Button>
                )}
                {managed && !managed.supported && (
                  <Button className="flex-1" size="md" color="brand" onClick={() => setStep('provider')}>
                    Use cloud
                  </Button>
                )}
                {managed?.status === 'failed' && (
                  <Button
                    className="flex-1"
                    size="md"
                    color="brand"
                    disabled={loading}
                    onClick={() => void beginManagedLocal()}
                  >
                    Try again
                  </Button>
                )}
              </SetupActions>
              <TextLink
                onClick={() => void loadLocalRuntimes()}
              >
                Use my own local server
              </TextLink>
            </div>
          )}

          {step === 'local_advanced' && (
            <div className="space-y-5">
              {localRuntimes.every((runtime) => !runtime.reachable) ? (
                <PanelSection className="border-status-warning/35 bg-status-warning/10 p-4 type-body text-foreground-muted">
                  No local runtimes detected. Install Ollama or start LM Studio / llama.cpp, then scan again.
                </PanelSection>
              ) : (
                <div className="scrollbar-thin grid max-h-56 gap-2 overflow-y-auto pr-1">
                  {localRuntimes
                    .filter((runtime) => runtime.reachable)
                    .map((runtime) => (
                      <button
                        key={runtime.runtime}
                        type="button"
                        aria-pressed={selectedLocalRuntime?.runtime === runtime.runtime}
                        onClick={() => {
                          setSelectedLocalRuntime(runtime)
                          setSelectedLocalModel(runtime.models[0] ?? '')
                        }}
                        className={cn(
                          'ui-surface-selectable min-h-14 rounded-control px-4 py-3 text-left transition-colors duration-feedback focus:outline-none',
                          selectedLocalRuntime?.runtime === runtime.runtime
                            ? 'ui-surface-selected'
                            : 'bg-surface/15 hover:bg-surface/25',
                        )}
                      >
                        <span className="type-label text-foreground">{runtime.label}</span>
                        <span className="mt-1 block type-meta text-foreground-muted">
                          {runtime.models.length
                            ? `${runtime.models.length} model(s) available`
                            : localRuntimeGuidance(runtime)}
                        </span>
                      </button>
                    ))}
                </div>
              )}

              {selectedLocalRuntime && selectedLocalRuntime.models.length > 0 && (
                <FieldControl label="Model">
                  <Select
                    aria-label="Model"
                    value={selectedLocalModel}
                    onChange={setSelectedLocalModel}
                    options={selectedLocalRuntime.models.map((name) => ({ value: name, label: name }))}
                  />
                </FieldControl>
              )}

              <SetupActions>
                <Button
                  variant="ghost"
                  color="neutral"
                  size="md"
                  className="min-w-24"
                  onClick={() => setStep('brain_choice')}
                >
                  Back
                </Button>
                <Button
                  variant="ghost"
                  color="neutral"
                  size="md"
                  className="min-w-32"
                  onClick={() => loadLocalRuntimes()}
                  disabled={loading}
                >
                  Scan again
                </Button>
                <Button
                  className="flex-1"
                  size="md"
                  color="brand"
                  disabled={!selectedLocalRuntime || !selectedLocalModel || loading}
                  onClick={() => void handleConnectExternalLocal()}
                >
                  {loading && <SpinnerIcon className="animate-spin" size={18} />}
                  {loading ? 'Connecting…' : 'Connect'}
                </Button>
              </SetupActions>
            </div>
          )}

          {step === 'provider' && (
            <div className="space-y-5">
              <div className="scrollbar-thin grid max-h-72 gap-2 overflow-y-auto pr-1">
                {CLOUD_MODEL_PRESETS.map((preset) => {
                  const p = providers.find((provider) => provider.id === preset.provider)
                  if (!p) return null
                  return (
                    <button
                      key={p.id}
                      type="button"
                      aria-pressed={selectedProvider === p.id}
                      onClick={() => setSelectedProvider(p.id)}
                      className={cn(
                        'ui-surface-selectable flex min-h-14 items-center justify-between gap-4 rounded-control px-4 py-3 text-left transition-colors duration-feedback focus:outline-none',
                        selectedProvider === p.id
                          ? 'ui-surface-selected'
                          : 'bg-surface/15 hover:bg-surface/25',
                      )}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block type-label text-foreground">{p.label}</span>
                        <span className="mt-1 block type-meta text-foreground-muted">{preset.subtitle}</span>
                      </span>
                      <span className="max-w-[48%] text-right">
                        <span className="block truncate type-meta text-foreground-muted">{p.recommended_model}</span>
                        {p.stability === 'preview' && (
                          <span className="mt-1 block type-fui text-status-warning">
                            Preview
                          </span>
                        )}
                      </span>
                    </button>
                  )
                })}
              </div>
              <SetupActions>
                <Button
                  variant="ghost"
                  color="neutral"
                  size="md"
                  className="min-w-28"
                  onClick={() => setStep('brain_choice')}
                >
                  Back
                </Button>
                <Button className="flex-1" size="md" color="brand" onClick={() => setStep('apikey')}>
                  Continue
                </Button>
              </SetupActions>
            </div>
          )}

          {step === 'apikey' && (
            <div className="space-y-5">
              <FieldControl
                label="API key"
                htmlFor="setup-api-key"
                hint={`Uses ${selectedPreset?.recommended_model ?? 'the recommended model'}.`}
              >
                <div className="relative">
                  <KeyIcon
                    size={16}
                    className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-foreground-subtle"
                    aria-hidden
                  />
                  <Input
                  id="setup-api-key"
                  type="password"
                  autoComplete="off"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={`Paste your ${selectedPreset?.label ?? 'provider'} key`}
                    className="pl-10"
                    invalid={Boolean(error)}
                  />
                </div>
                {selectedPreset?.signup_url && (
                  <TextLink
                    href={selectedPreset.signup_url}
                    external
                    className="type-meta"
                  >
                    Get a {selectedPreset.label} API key
                  </TextLink>
                )}
              </FieldControl>
              <SetupActions>
                <Button
                  variant="ghost"
                  color="neutral"
                  size="md"
                  className="min-w-28"
                  onClick={() => setStep('provider')}
                >
                  Back
                </Button>
                <Button
                  className="flex-1"
                  size="md"
                  color="brand"
                  disabled={!apiKey.trim() || loading}
                  onClick={() => void handleConnectCloud()}
                >
                  {loading && <SpinnerIcon className="animate-spin" size={18} />}
                  {loading ? 'Connecting…' : 'Connect'}
                </Button>
              </SetupActions>
            </div>
          )}

          {step === 'voice_profile' && (
            <div className="space-y-5">
              <OwnerVoiceEnrollment
                variant="setup"
                onEnrolled={() => setStep('ready')}
              />
              <SetupActions>
                <Button
                  className="flex-1"
                  size="md"
                  variant="ghost"
                  color="neutral"
                  onClick={() => setStep('ready')}
                >
                  Not now
                </Button>
              </SetupActions>
            </div>
          )}

          {step === 'ready' && (
            <div className="space-y-4 text-center">
              <CheckCircleIcon className="mx-auto text-status-success" size={48} />
              <p className="type-body text-foreground-muted">
                Try typing <span className="text-foreground font-medium">{STARTER_PROMPT}</span>
                {' '}or use the microphone button below.
              </p>
              <Button className="w-full" size="md" color="brand" onClick={onComplete}>
                Continue to JARV1S
              </Button>
            </div>
          )}

            {error && (
              <div
                className="space-y-3 rounded-control border border-status-danger/35 bg-status-danger/10 p-4 text-left"
                role="alert"
              >
                <p className="type-body text-status-danger-fg">
                  {error}
                  {validation?.next_action ? ` ${validation.next_action}` : ''}
                </p>
                {validation?.code === 'model_unavailable'
                  && validation.recommended_model
                  && step === 'apikey'
                  && validation.recommended_model !== selectedPreset?.recommended_model && (
                  <Button
                    color="neutral"
                    size="sm"
                    disabled={loading}
                    onClick={() => void handleConnectCloud(validation.recommended_model ?? undefined)}
                  >
                    Use {validation.recommended_model}
                  </Button>
                )}
              </div>
            )}
          </div>
        </Hologram>
      </div>
    </main>
  )
}
