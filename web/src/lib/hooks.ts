/**
 * Polling (CODING_GUIDELINES §4.5, §4.6, §9.5, §9.9, §9.12). A page asks again every POLL_MS only
 * while shouldPoll says the server is still working, and only while the tab is visible: hidden, it
 * waits, and asks again as soon as it's shown. Each load is retried, so one dropped request never
 * stops the polling. Leaving the page stops it: the request is aborted, the timer cleared, and an
 * answer that still arrives is ignored.
 */
import { type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import type { Page, Shop } from "../types/api";
import { ABORTED, ApiError, listShops, messageOf, recordingDeleted, signOut } from "./api";
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
    // Due while the tab was hidden: asked again as soon as it's shown (§9.12).
    let waiting = false;
    const visible = () => document.visibilityState === "visible";
    const next = () => {
      timer = undefined;
      if (visible()) void run();
      else waiting = true;
    };
    const onShown = () => {
      if (!visible() || !waiting) return;
      waiting = false;
      void run();
    };
    const run = async () => {
      try {
        const value = await withRetry(() => latest.current.load(controller.signal), {
          attempts: READ_ATTEMPTS,
          signal: controller.signal,
        });
        if (!alive) return;
        setLoaded({ key, data: value, error: null });
        const { shouldPoll, intervalMs = POLL_MS } = latest.current.options;
        if (shouldPoll(value)) timer = setTimeout(next, intervalMs);
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
    document.addEventListener("visibilitychange", onShown);
    return () => {
      alive = false;
      controller.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onShown);
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

/** Sign out, then on to sign in: the header's, and the profile's (batch 17e). */
export function useSignOut(): { leave: () => Promise<void>; busy: boolean; failed: string | null } {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const leave = async () => {
    setBusy(true);
    setFailed(null);
    try {
      await signOut();
      navigate("/signin", { replace: true });
    } catch (error) {
      setFailed(messageOf(error, "Signing out didn't work. Try again."));
      setBusy(false);
    }
  };
  return { leave, busy, failed };
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

/** A page's one player (§9.11): ▶ on a quote or a line plays from its moment, one at a time. */
export interface Player {
  /** For the page's <audio>. */
  audio: RefObject<HTMLAudioElement | null>;
  onError: () => void;
  onLoadedMetadata: () => void;
  /** The recording at `src` couldn't be loaded (its file lost, say): the page says so. */
  unplayable: boolean;
  /** And the server said why: it was deleted under the retention policy (batch 17), so trying
   * again can't help. */
  deleted: boolean;
  /** The message's Try again: ▶ moves to it, so it's read out again. */
  retry: RefObject<HTMLButtonElement | null>;
  tryAgain: () => void;
  /** The recording it plays: the page's own, or the one last asked for (playFrom). */
  src: string | null;
  /** The current recording from `ms`. */
  playAt: (ms: number) => void;
  /** Another page's way: `url`'s recording from `ms`, once it's loaded if it's a new one. */
  playFrom: (url: string, ms: number) => void;
}

/** The player for a visit's recording, `fixed` (batch 14), or for whichever one an inbox page
 * last asked for (batch 15). A recording that can't be loaded never fails silently. */
export function usePlayer(fixed: string | null = null): Player {
  const audio = useRef<HTMLAudioElement>(null);
  const retry = useRef<HTMLButtonElement>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const src = fixed ?? chosen;
  const [failed, setFailed] = useState<string | null>(null);
  const [deleted, setDeleted] = useState<string | null>(null);
  // A start asked for before its recording was loaded: onLoadedMetadata plays it.
  const pending = useRef<number | null>(null);
  const start = useCallback((ms: number) => {
    const player = audio.current;
    if (player === null) return;
    player.currentTime = ms / 1000;
    player.play().catch(() => {}); // a browser that refuses to play still moved to the moment
  }, []);
  const playAt = useCallback(
    (ms: number) => {
      const player = audio.current;
      if (player === null) return;
      if (player.error !== null) {
        setFailed(src);
        retry.current?.focus(); // the message, read out again, and in view
        return;
      }
      start(ms);
    },
    [src, start],
  );
  const playFrom = useCallback(
    (url: string, ms: number) => {
      if (url === src) {
        playAt(ms);
        return;
      }
      // A new recording: the page renders its player with it, and it plays once loaded.
      pending.current = ms;
      setChosen(url);
    },
    [src, playAt],
  );
  const onLoadedMetadata = useCallback(() => {
    if (pending.current === null) return;
    const ms = pending.current;
    pending.current = null;
    start(ms);
  }, [start]);
  const onError = useCallback(() => {
    pending.current = null;
    setFailed(src); // at once: an answer that never comes must not leave the page silent
    if (src === null) return;
    void recordingDeleted(src).then((gone) => {
      if (gone) setDeleted(src);
    });
  }, [src]);
  const tryAgain = useCallback(() => {
    setFailed(null);
    audio.current?.load(); // a failure again sets the message again
  }, []);
  return {
    audio,
    onError,
    onLoadedMetadata,
    unplayable: src !== null && failed === src,
    deleted: src !== null && deleted === src,
    retry,
    tryAgain,
    src,
    playAt,
    playFrom,
  };
}
