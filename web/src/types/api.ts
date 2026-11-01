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

/** One quote behind an insight: countertalk/api/insights.py, QuoteOut. */
export interface Quote {
  seq: number;
  turn_id: number | null;
  speaker_role: string | null;
  /** The transcript's own words where it was found; the model's where it wasn't. */
  text: string;
  match: "exact" | "fuzzy" | "none" | null;
  /** Where ▶ starts playing, in visit time; null for a quote that wasn't found. */
  start_ms: number | null;
}

/** One team an insight is sent to: RouteOut. */
export interface Route {
  slot: number;
  team_key: string;
  team_name: string;
  routed_by: "table" | "ai" | "override" | "person";
}

/** InsightOut. An unverified insight was never found in the recording: never routed or shown. */
export interface Insight {
  id: string;
  seq: number;
  kind: string;
  category: string;
  severity: string;
  summary_en: string;
  verified: boolean;
  visible: boolean;
  added_by_rep: boolean;
  /** The linked catalog items' names. */
  sku: string | null;
  competitor: string | null;
  scheme_key: string | null;
  /** The AI's suggested team's key, and why. */
  ai_team: string | null;
  ai_team_reason: string | null;
  quotes: Quote[];
  routes: Route[];
}

/**
 * PATCH /api/insights/{id}: InsightChange. 400 `invalid_category`, `invalid_severity`,
 * `summary_empty`, `summary_too_long`; 404 `insight_not_found`; 409 `insight_unverified`,
 * `visit_confirmed`, `visit_not_in_review`, `routed_by_hand`.
 */
export interface InsightChange {
  summary_en?: string;
  category?: string;
  severity?: string;
}

/**
 * POST /api/visits/{id}/insights: NewInsight. 400 `invalid_kind`, `invalid_category`,
 * `invalid_severity`, `summary_empty`, `summary_too_long`; 404 `turn_not_found`; 409 as PATCH.
 */
export interface NewInsight {
  turn_id: number;
  kind: string;
  category: string;
  severity: string;
  summary_en: string;
}

/** POST /api/visits/{id}/notes: NewNote. 400 `note_empty`, `note_too_long`; 409 as PATCH. */
export interface NewNote {
  text: string;
}

/** NoteOut: for the visit's record only, never sent to teams. */
export interface Note {
  id: string;
  text: string;
  created_at: string;
}

/** ReportOut. */
export interface Report {
  summary_en: string;
  /** YYYY-MM-DD. */
  next_visit_date: string | null;
}

/** TaskOut. */
export interface Task {
  seq: number;
  type: string;
  description_en: string;
  /** YYYY-MM-DD. */
  due: string | null;
  owner: string;
  status: "open" | "done";
}

/** ChoiceOut: one of the playbook's keys, with its label. */
export interface Choice {
  key: string;
  label: string;
}

/**
 * GET /api/visits/{id}/review: ReviewOut, everything the review page shows. Edits, adds and
 * notes give 409 `visit_confirmed` after confirm, and `visit_not_in_review` before review.
 */
export interface Review {
  visit: Visit;
  report: Report | null;
  tasks: Task[];
  insights: Insight[];
  notes: Note[];
  kinds: Choice[];
  categories: Choice[];
  severities: Choice[];
}

/** The longest note the server takes: countertalk/limits.py, NOTE_MAX_CHARS. */
export const NOTE_MAX_CHARS = 2_000;

/** The longest summary a rep may write: countertalk/limits.py, SUMMARY_MAX_CHARS. */
export const SUMMARY_MAX_CHARS = 500;
