/**
 * Polling (CODING_GUIDELINES §4.5, §4.6, §9.5, §9.9, §9.12). A page asks again every POLL_MS only
 * while shouldPoll says the server is still working. Each load is retried, so one dropped request
 * never stops the polling. Leaving the page stops it: the request is aborted, the timer cleared,
 * and an answer that still arrives is ignored.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ABORTED, ApiError } from "./api";
import { POLL_MS, READ_ATTEMPTS } from "./limits";
import { withRetry } from "./retry";

export interface PollingOptions<T> {
  /** A change (a new visit ID, say) starts over, and the old key's data is never shown. */
  key: unknown;
  shouldPoll: (value: T) => boolean;
  intervalMs?: number;
}

export interface Polling<T> {
  data: T | undefined;
  error: ApiError | null;
  /** Starts again after an error: the way forward behind a Try again button. */
  reload: () => void;
}

type Loaded<T> = { key: unknown; data?: T; error: ApiError | null };

export function usePolling<T>(
  load: (signal: AbortSignal) => Promise<T>,
  options: PollingOptions<T>,
): Polling<T> {
  const { key } = options;
  const [loaded, setLoaded] = useState<Loaded<T>>({ key, error: null });
  const [attempt, setAttempt] = useState(0);
  // The latest functions, so a new closure each render doesn't restart the polling.
  const latest = useRef({ load, options });
  useEffect(() => {
    latest.current = { load, options };
  });

  useEffect(() => {
    let alive = true;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const run = async () => {
      try {
        const value = await withRetry(() => latest.current.load(controller.signal), {
          attempts: READ_ATTEMPTS,
          signal: controller.signal,
        });
        if (!alive) return;
        setLoaded({ key, data: value, error: null });
        const { shouldPoll, intervalMs = POLL_MS } = latest.current.options;
        if (shouldPoll(value)) timer = setTimeout(() => void run(), intervalMs);
      } catch (failure) {
        if (!alive || (failure instanceof ApiError && failure.code === ABORTED)) return;
        const error =
          failure instanceof ApiError
            ? failure
            : new ApiError(0, "unexpected_error", "Something went wrong while loading.");
        setLoaded((previous) => ({ key, data: previous.key === key ? previous.data : undefined, error }));
      }
    };
    void run();
    return () => {
      alive = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [key, attempt]);

  const reload = useCallback(() => {
    setLoaded((previous) => ({ ...previous, error: null }));
    setAttempt((count) => count + 1);
  }, []);

  const current = loaded.key === key;
  return { data: current ? loaded.data : undefined, error: current ? loaded.error : null, reload };
}
