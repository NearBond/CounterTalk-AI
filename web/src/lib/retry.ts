/**
 * Retries with backoff and jitter (CODING_GUIDELINES §9.5, §9.11): the one place the web app
 * waits and tries again. Only passing failures are retried: no answer, 408, 429 and 5xx.
 */
import { ABORTED, ApiError, abortedError } from "./api";
import { RETRY_BASE_MS, RETRY_JITTER, RETRY_MAX_MS } from "./limits";

export type Sleep = (ms: number, signal?: AbortSignal) => Promise<void>;

export interface RetryOptions {
  /** Tries in all, the first included. Infinity for work that must go through (UploadQueue). */
  attempts: number;
  /** Aborting it ends the wait at once; the call then rejects as `aborted`. */
  signal?: AbortSignal;
  /** Called before each wait: for showing "retrying". */
  onRetry?: (error: unknown, attempt: number) => void;
  random?: () => number;
  sleep?: Sleep;
}

export function isRetryable(error: unknown): boolean {
  if (!(error instanceof ApiError) || error.code === ABORTED) return false;
  return error.status === 0 || error.status === 408 || error.status === 429 || error.status >= 500;
}

/** The wait after the attempt-th failed try: doubling from the base, capped, then jittered. */
export function backoffMs(attempt: number, random: () => number = Math.random): number {
  const delay = Math.min(RETRY_BASE_MS * 2 ** (attempt - 1), RETRY_MAX_MS);
  return Math.round(delay * (1 - RETRY_JITTER + 2 * RETRY_JITTER * random()));
}

/** Waits, or rejects as `aborted` as soon as the signal is (or already was) aborted. */
export const sleep: Sleep = (ms, signal) =>
  new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortedError());
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortedError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });

export async function withRetry<T>(call: () => Promise<T>, options: RetryOptions): Promise<T> {
  const wait = options.sleep ?? sleep;
  for (let attempt = 1; ; attempt += 1) {
    try {
      return await call();
    } catch (error) {
      // After an abort, the call rejects as `aborted` (not retryable), or the wait does.
      if (!isRetryable(error) || attempt >= options.attempts) throw error;
      options.onRetry?.(error, attempt);
      await wait(backoffMs(attempt, options.random), options.signal);
    }
  }
}
