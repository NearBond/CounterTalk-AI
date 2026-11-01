/** Helpers for sending a visit's audio (BRIEF §2): hashing, the Upload panel's byte ranges, and
 * `complete`, which finishes the rep's work and so is always retried (§9.11). */
import {
  ALLOWED_EXTS,
  MAX_SEGMENT_BYTES,
  type RecordingExt,
  type Visit,
  type VisitClient,
} from "../types/api";
import { createVisit, postComplete, putSegment } from "./api";
import { COMPLETE_ATTEMPTS } from "./limits";
import { withRetry } from "./retry";
import { type QueueState, UploadQueue } from "./uploadQueue";

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

/** Waits for every segment to be uploaded, then completes the visit: what Stop and the Upload
 * panel both end with. Rejects with the queue's failure, or complete's after its retries. */
export async function finishVisit(
  queue: UploadQueue,
  visitId: string,
  lastSeq: number,
  ext: RecordingExt,
  signal?: AbortSignal,
): Promise<Visit> {
  await queue.drain();
  return completeVisit(visitId, lastSeq, ext, signal);
}

/**
 * Creates the visit and the queue that sends its segments: how a recording and an upload start.
 * Not retried: a POST whose answer was lost may have made the visit, and a retry would make a
 * second one. The rep presses Start (or Send) again instead.
 */
export async function openVisit(
  shopId: string,
  client: VisitClient,
  onChange: (state: QueueState) => void,
  signal: AbortSignal,
): Promise<{ visitId: string; queue: UploadQueue }> {
  const visit = await createVisit({ shop_id: shopId, kind: "conversation", client }, signal);
  const queue = new UploadQueue({
    upload: (seq, body, sha256, uploadSignal) => putSegment(visit.id, seq, body, sha256, uploadSignal),
    onChange,
  });
  return { visitId: visit.id, queue };
}
