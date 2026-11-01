/**
 * Polling (CODING_GUIDELINES §4.5, §4.6, §9.5, §9.9, §9.12). A page asks again every POLL_MS only
 * while shouldPoll says the server is still working. Each load is retried, so one dropped request
 * never stops the polling. Leaving the page stops it: the request is aborted, the timer cleared,
 * and an answer that still arrives is ignored.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Page, Shop } from "../types/api";
import { ABORTED, ApiError, listShops } from "./api";
import { POLL_MS, READ_ATTEMPTS } from "./limits";
import { withRetry } from "./retry";

export interface PollingOptions<T> {
  /** A change (a new visit ID, say) starts over, and the old key's data is never shown. */
  key: unknown;
  shouldPoll: (value: T) => boolean;
  intervalMs?: number;
  /** Nothing to load yet (a transcript before transcription): no request, no data. */
  skip?: boolean;
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
  const { key, skip = false } = options;
  const [loaded, setLoaded] = useState<Loaded<T>>({ key, error: null });
  const [attempt, setAttempt] = useState(0);
  // The latest functions, so a new closure each render doesn't restart the polling.
  const latest = useRef({ load, options });
  useEffect(() => {
    latest.current = { load, options };
  });

  useEffect(() => {
    if (skip) return;
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
  }, [key, attempt, skip]);

  const reload = useCallback(() => {
    setLoaded((previous) => ({ ...previous, error: null }));
    setAttempt((count) => count + 1);
  }, []);

  const current = loaded.key === key;
  return { data: current ? loaded.data : undefined, error: current ? loaded.error : null, reload };
}

/** The org's shops, loaded once per page with retries; `reload` after an error. */
export function useShops(): Polling<Page<Shop>> {
  return usePolling((signal) => listShops(signal), { key: "shops", shouldPoll: () => false });
}

/** Milliseconds since `since` (a Date.now() value), ticking every second; 0 while it's null. */
export function useElapsed(since: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (since === null) return;
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [since]);
  return since === null ? 0 : Math.max(0, now - since);
}

/** One save at a time (§9.11, §9.13). */
export interface Save {
  /** True while it's in flight: its controls are disabled. */
  saving: boolean;
  error: ApiError | null;
  /** Sends `work` with a signal that leaving the page aborts; `onDone` gets its answer only while
   * the page is still there (§9.9). */
  run: <T>(work: (signal: AbortSignal) => Promise<T>, onDone: (value: T) => void) => void;
  /** Sends the last work again, as it was: for work with no inputs to change, like confirm. */
  retry: () => void;
}

export function useSave(): Save {
  const alive = useRef(true);
  const controller = useRef(new AbortController());
  useEffect(() => {
    alive.current = true;
    controller.current = new AbortController();
    return () => {
      alive.current = false;
      controller.current.abort();
    };
  }, []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const last = useRef<(() => void) | null>(null);

  const run = useCallback(<T>(work: (signal: AbortSignal) => Promise<T>, onDone: (value: T) => void) => {
    const attempt = () => {
      setSaving(true);
      setError(null);
      void (async () => {
        try {
          const value = await work(controller.current.signal);
          if (!alive.current) return;
          setSaving(false);
          onDone(value);
        } catch (failure) {
          if (!alive.current) return;
          setSaving(false);
          setError(
            failure instanceof ApiError
              ? failure
              : new ApiError(0, "unexpected_error", "Something went wrong while saving."),
          );
        }
      })();
    };
    last.current = attempt;
    attempt();
  }, []);
  const retry = useCallback(() => last.current?.(), []);
  return { saving, error, run, retry };
}

/** While `dirty`, closing or reloading the page asks first (§9.9). */
export function useLeaveWarning(dirty: boolean): void {
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = ""; // older Chrome and WebView ask only when it's set
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
}
