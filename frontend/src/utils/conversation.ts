export const CONVERSATION_INACTIVITY_MS = 2 * 60 * 60_000

export function startsNewConversation(previousTimestamp: number, timestamp: number): boolean {
  return timestamp - previousTimestamp > CONVERSATION_INACTIVITY_MS
}
