/** Helpers for sending a visit's audio (BRIEF §2): hashing, the Upload panel's byte ranges, and
 * `complete`, which finishes the rep's work and so is always retried (§9.11). */
import { ALLOWED_EXTS, MAX_SEGMENT_BYTES, type RecordingExt, type Visit } from "../types/api";
import { postComplete } from "./api";
import { COMPLETE_ATTEMPTS } from "./limits";
import { withRetry } from "./retry";

/** The SHA-256 the server checks each segment against, as 64 lowercase hex digits. */
export async function sha256Hex(body: Blob): Promise<string> {
  // A view, not the buffer itself: a buffer from another realm (a test's DOM) would be refused.
  const digest = await crypto.subtle.digest("SHA-256", new Uint8Array(await body.arrayBuffer()));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** A file's segments: byte ranges [start, end) of at most the server's segment limit. */
export function byteRanges(size: number, limit: number = MAX_SEGMENT_BYTES): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  for (let start = 0; start < size; start += limit) ranges.push([start, Math.min(start + limit, size)]);
  return ranges;
}

/** The recording's type from its file name, if the server takes it. */
export function extOf(fileName: string): RecordingExt | null {
  const dot = fileName.lastIndexOf(".");
  if (dot <= 0) return null;
  const ext = fileName.slice(dot + 1).toLowerCase();
  return (ALLOWED_EXTS as readonly string[]).includes(ext) ? (ext as RecordingExt) : null;
}

/** Marks the visit uploaded, retried for about a minute; the caller then offers Try again. */
export function completeVisit(
  visitId: string,
  lastSeq: number,
  ext: RecordingExt,
  signal?: AbortSignal,
): Promise<Visit> {
  return withRetry(() => postComplete(visitId, { last_seq: lastSeq, ext }, signal), {
    attempts: COMPLETE_ATTEMPTS,
    signal,
  });
}
