export type ModelPresetId = 'fast' | 'balanced' | 'budget' | 'local'

export interface ModelPreset {
  id: ModelPresetId
  label: string
  provider: string
  model?: string
  subtitle: string
}

export const MODEL_PRESETS: ModelPreset[] = [
  {
    id: 'fast',
    label: 'Fastest',
    provider: 'cerebras',
    subtitle: 'Lowest latency · preview',
  },
  {
    id: 'balanced',
    label: 'Balanced',
    provider: 'google-ai-studio',
    subtitle: 'Recommended for most requests',
  },
  {
    id: 'budget',
    label: 'Flexible',
    provider: 'openrouter',
    subtitle: 'Choose models through OpenRouter',
  },
  {
    id: 'local',
    label: 'Private',
    provider: 'ollama',
    subtitle: 'Runs on this Mac · no API key',
  },
]

export const CLOUD_MODEL_PRESETS = MODEL_PRESETS.filter((preset) => preset.id !== 'local')

export function getModelPreset(id: ModelPresetId): ModelPreset | undefined {
  return MODEL_PRESETS.find((preset) => preset.id === id)
}

export function formatModelLabel(model: string | null | undefined): string {
  if (!model) return 'Unknown model'
  const slug = model.split('/').pop() ?? model
  return slug.replace(/-/g, ' ')
}

export function formatProviderLabel(provider: string | null | undefined): string {
  if (!provider) return 'Unknown provider'
  return provider.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

export function modelsMatch(a: string | null | undefined, b: string | null | undefined): boolean {
  if (!a || !b) return false
  return a.toLowerCase() === b.toLowerCase()
}

export function matchActivePreset(
  provider: string | null | undefined,
  model: string | null | undefined,
  providers: { id: string; default_model: string }[],
  managedModel?: string | null,
): ModelPresetId | null {
  if (!provider) return null
  for (const preset of MODEL_PRESETS) {
    if (preset.id === 'local') {
      if (provider === 'ollama' && modelsMatch(model, managedModel)) {
        return 'local'
      }
      continue
    }
    if (preset.provider !== provider) continue
    const expectedModel = preset.model ?? providers.find((p) => p.id === preset.provider)?.default_model
    if (!expectedModel || modelsMatch(model, expectedModel)) return preset.id
  }
  return null
}
