/**
 * REST client for owner runtime preferences.
 */

import { authorizedJson } from './http'

export interface JarvisPreferences {
  owner_id: string
  audio: {
    tool_cues_enabled: boolean
  }
}

export const preferencesApi = {
  getPreferences: () => authorizedJson<JarvisPreferences>('/api/v1/preferences/'),
  setToolCuesEnabled: (enabled: boolean) =>
    authorizedJson<JarvisPreferences>('/api/v1/preferences/', {
      method: 'PATCH',
      body: JSON.stringify({
        audio: { tool_cues_enabled: enabled },
      }),
    }),
}
