import { describe, expect, it } from 'vitest'

import { CLOUD_MODEL_PRESETS, matchActivePreset } from './modelPresets'

const providers = [
  { id: 'cerebras', default_model: 'gemma-4-31b' },
  { id: 'google-ai-studio', default_model: 'gemini-3.5-flash' },
  { id: 'openrouter', default_model: 'google/gemma-4-26b-a4b-it' },
]

describe('model presets', () => {
  it('keeps the primary cloud path curated', () => {
    expect(CLOUD_MODEL_PRESETS.map((preset) => preset.provider)).toEqual([
      'cerebras',
      'google-ai-studio',
      'openrouter',
    ])
  })

  it('defers cloud model ids to the backend catalog', () => {
    expect(CLOUD_MODEL_PRESETS.every((preset) => !preset.model)).toBe(true)
  })

  it('matches the Google AI Studio recommended model', () => {
    expect(matchActivePreset('google-ai-studio', 'gemini-3.5-flash', providers)).toBe('balanced')
  })

  it('distinguishes managed On this Mac from external runtimes', () => {
    expect(matchActivePreset('ollama', 'managed-model', providers, 'managed-model')).toBe('local')
    expect(matchActivePreset('ollama', 'other-model', providers, 'managed-model')).toBeNull()
    expect(matchActivePreset('lmstudio', 'local-model', providers, 'managed-model')).toBeNull()
  })
})
