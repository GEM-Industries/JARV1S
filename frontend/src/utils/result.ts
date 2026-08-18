/**
 * Result type for functional error handling without try/catch.
 */
export type Result<T, E = string> = 
  | { ok: true; value: T }
  | { ok: false; error: E }

export const ok = <T>(value: T): Result<T, never> => ({ ok: true, value })
export const err = <E = string>(error: E): Result<never, E> => ({ ok: false, error })

/**
 * Wrap a promise to return a Result instead of throwing.
 */
export async function tryCatch<T>(promise: Promise<T>): Promise<Result<T>> {
  const result = await promise.catch((e: Error) => e)
  if (result instanceof Error) {
    return err(result.message)
  }
  return ok(result as T)
}
