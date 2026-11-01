/**
 * The web app's timings and counts (CODING_GUIDELINES §1.6). The server's own limits that the
 * app must respect are mirrored in types/api.ts, where a backend test checks them.
 */

/** MediaRecorder hands over a segment this often (BRIEF §2). */
export const SEGMENT_MS = 30_000;

/** How often a page asks again about a visit, or the list, while it's processing (§9.12). */
export const POLL_MS = 3_000;

/** A retry waits RETRY_BASE_MS * 2^(attempt - 1), at most RETRY_MAX_MS, give or take JITTER. */
export const RETRY_BASE_MS = 1_000;
export const RETRY_MAX_MS = 30_000;
export const RETRY_JITTER = 0.2;

/** Tries for `complete`, which finishes the rep's work: about 30 s of waits, then Try again (§9.11). */
export const COMPLETE_ATTEMPTS = 6;

/** Tries for each of a page's reads (a visit, a transcript, the list) before it shows an error:
 * about 7 s of waits, so one dropped request never stops a page's polling (§9.5). */
export const READ_ATTEMPTS = 4;
