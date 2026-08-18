export interface AuthOAuthChangedEvent {
  app: string
  success: boolean
  loaded?: boolean
  kind?: 'bespoke' | 'composio'
}

const listeners = new Set<(event: AuthOAuthChangedEvent) => void>()

export function subscribeAuthOAuthChanged(
  listener: (event: AuthOAuthChangedEvent) => void,
): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function dispatchAuthOAuthChanged(event: AuthOAuthChangedEvent): void {
  for (const listener of listeners) {
    listener(event)
  }
}
