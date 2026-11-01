/**
 * The API's types, written by hand to mirror the backend's schemas (CODING_GUIDELINES §4.4).
 * Field names are snake_case, exactly as the API sends them.
 */

/**
 * GET /api/health: countertalk/api/health.py, HealthOut. Sent with 200 when everything is ok,
 * and with 503 when the database can't be reached.
 */
export interface Health {
  status: "ok" | "error";
  database: "ok" | "error";
}

/** The framework's own error codes (countertalk/main.py). Domain errors bring their own. */
export type FrameworkErrorCode =
  | "not_found"
  | "method_not_allowed"
  | "invalid_request"
  | "internal_error";

/** The details of an invalid_request (422): where and why, never the values sent. */
export interface InvalidRequestDetails {
  errors: Array<{ loc: Array<string | number>; msg: string; type: string }>;
}

/** POST /api/visits/{id}/complete, 409 `segments_missing`. */
export interface SegmentsMissingDetails {
  missing_seqs: number[];
}

/** POST /api/visits/{id}/complete, 409 `unexpected_segments`: segments past `last_seq`. */
export interface UnexpectedSegmentsDetails {
  unexpected_seqs: number[];
}

/** PUT /api/visits/{id}/segments/{seq}, 409 `segment_conflict`: other bytes were stored. */
export interface SegmentConflictDetails {
  seq: number;
}

/** Every error response (§2.10). */
export interface ErrorBody {
  error: {
    code: FrameworkErrorCode | (string & {});
    message: string;
    details?:
      | InvalidRequestDetails
      | SegmentsMissingDetails
      | UnexpectedSegmentsDetails
      | SegmentConflictDetails
      | Record<string, unknown>;
  };
}

/** Every list endpoint (§2.11). */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

/** GET /api/shops: countertalk/api/shops.py, ShopOut. */
export interface Shop {
  id: string;
  name: string;
  area: string;
  do_not_record: boolean;
}

export type VisitStatus =
  | "recording"
  | "uploaded"
  | "transcribing"
  | "transcribed"
  | "extracting"
  | "review"
  | "confirmed"
  | "failed";

export type VisitKind = "conversation" | "notes";

export type VisitClient = "web" | "upload" | "phone";

/** countertalk/api/visits.py, VisitOut. Times are ISO 8601 in UTC, ending in Z. */
export interface Visit {
  id: string;
  shop_id: string;
  rep_id: string;
  kind: VisitKind;
  client: VisitClient;
  status: VisitStatus;
  failure_reason: string | null;
  duration_ms: number | null;
  /** True while it has a queued, running or retrying job: poll until it's false. */
  processing: boolean;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
}

/** POST /api/visits: VisitIn. */
export interface NewVisit {
  shop_id: string;
  kind: VisitKind;
  client: VisitClient;
}

/** The types an uploaded recording may have: countertalk/storage.py, ALLOWED_EXTS. */
export const ALLOWED_EXTS = ["webm", "m4a", "mp4", "aac", "ogg", "wav"] as const;
export type RecordingExt = (typeof ALLOWED_EXTS)[number];

/** The largest segment the server takes: countertalk/limits.py, MAX_SEGMENT_BYTES (8 MiB). */
export const MAX_SEGMENT_BYTES = 8_388_608;

/** POST /api/visits/{id}/complete: CompleteIn. */
export interface CompleteVisit {
  last_seq: number;
  ext: RecordingExt;
}

/**
 * PUT /api/visits/{id}/segments/{seq}: SegmentOut. The body is the segment's raw bytes, sent
 * with Content-Length and X-Content-SHA256 (64 lowercase hex); 201 is new, 200 the same again.
 */
export interface Segment {
  seq: number;
  bytes: number;
  sha256: string;
  received_at: string;
}

/** One speaker's turn, in visit time: countertalk/api/visits.py, TurnOut. */
export interface Turn {
  turn_id: number;
  /** Speech-to-text's label, prefixed with its chunk: "c0:A", "c1:A". */
  speaker_label: string;
  start_ms: number;
  end_ms: number;
  text: string;
}

/**
 * GET /api/visits/{id}/transcript: TranscriptOut, 404 `transcript_not_found` until there is
 * one. The audio is GET /api/visits/{id}/audio (audio/mp4, seekable with Range requests).
 */
export interface Transcript {
  id: string;
  visit_id: string;
  provider: string;
  model: string;
  /** The languages recognition was biased towards, as ISO 639-1 codes ("hi,en"), or null. */
  language_code: string | null;
  created_at: string;
  turns: Turn[];
}
