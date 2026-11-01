/**
 * The only place that calls fetch (CODING_GUIDELINES §4.3). Every failure becomes an ApiError,
 * so a caller has one thing to catch and always gets a status, a code and a readable message.
 */
import type {
  CompleteVisit,
  ErrorBody,
  FrameworkErrorCode,
  Health,
  Insight,
  InsightChange,
  NewInsight,
  NewNote,
  NewVisit,
  Note,
  Page,
  Review,
  Segment,
  Shop,
  Transcript,
  Visit,
} from "../types/api";

const BASE_URL = "/api";

/** No answer arrived, or it broke off: offline, the server is down. Status 0. */
export const NETWORK_ERROR = "network_error";
/** The caller cancelled the request through its signal. Status 0. */
export const ABORTED = "aborted";
/** An answer the app can't read: an HTML error page, an empty body, or JSON in another shape. */
export const UNEXPECTED_RESPONSE = "unexpected_response";

export type ApiErrorCode =
  | FrameworkErrorCode
  | typeof NETWORK_ERROR
  | typeof ABORTED
  | typeof UNEXPECTED_RESPONSE
  | (string & {});

export class ApiError extends Error {
  /** The HTTP status, or 0 when no answer arrived. */
  readonly status: number;
  readonly code: ApiErrorCode;
  readonly details: ErrorBody["error"]["details"];
  /** The parsed JSON body, whatever its shape, so nothing the server said is lost (§9.6). */
  readonly body: unknown;

  constructor(
    status: number,
    code: ApiErrorCode,
    message: string,
    details?: ErrorBody["error"]["details"],
    body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.body = body;
  }
}

/** The error for work the caller cancelled: one shape wherever it's made. */
export function abortedError(message = "The request was cancelled."): ApiError {
  return new ApiError(0, ABORTED, message);
}

/** What to show for a failure: our own message, or a fallback for anything else. */
export function messageOf(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  /** Sent as JSON. */
  body?: unknown;
  /** Sent as they are, instead of a JSON body: a segment's audio. */
  bytes?: Blob;
  headers?: Record<string, string>;
  /** Aborting it cancels the request; the caller gets an ApiError with code `aborted`. */
  signal?: AbortSignal;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers({ Accept: "application/json", ...options.headers });
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (options.bytes !== undefined) headers.set("Content-Type", "application/octet-stream");

  let response: Response;
  let text: string;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      method: options.method ?? "GET",
      headers,
      body: options.bytes ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
      signal: options.signal,
    });
    // Inside the same try: a body that breaks off part-way is a network failure too.
    text = await response.text();
  } catch {
    if (options.signal?.aborted) throw abortedError();
    throw new ApiError(0, NETWORK_ERROR, "Can't reach the server. Check the connection and try again.");
  }

  const body = parseJson(text);
  if (!response.ok) throw errorFrom(response.status, body);
  if (response.status === 204) return undefined as T;
  if (body === NOT_JSON) {
    throw new ApiError(
      response.status,
      UNEXPECTED_RESPONSE,
      `The server's answer (HTTP ${response.status}) wasn't JSON.`,
    );
  }
  return body as T;
}

export async function getHealth(signal?: AbortSignal): Promise<Health> {
  try {
    return await apiRequest<Health>("/health", { signal });
  } catch (error) {
    // The health check answers 503 with its usual body when the database is down: that's an
    // answer, not a failure to get one.
    if (error instanceof ApiError && error.status === 503 && isHealth(error.body)) return error.body;
    throw error;
  }
}

export function listShops(signal?: AbortSignal): Promise<Page<Shop>> {
  return apiRequest("/shops", { signal });
}

export function listVisits(signal?: AbortSignal): Promise<Page<Visit>> {
  return apiRequest("/visits", { signal });
}

export function getVisit(visitId: string, signal?: AbortSignal): Promise<Visit> {
  return apiRequest(`/visits/${encodeURIComponent(visitId)}`, { signal });
}

export function createVisit(body: NewVisit, signal?: AbortSignal): Promise<Visit> {
  return apiRequest("/visits", { method: "POST", body, signal });
}

/** 201 for a new segment, 200 for the same bytes again: safe to repeat. */
export function putSegment(
  visitId: string,
  seq: number,
  bytes: Blob,
  sha256: string,
  signal?: AbortSignal,
): Promise<Segment> {
  return apiRequest(`/visits/${encodeURIComponent(visitId)}/segments/${seq}`, {
    method: "PUT",
    bytes,
    headers: { "X-Content-SHA256": sha256 },
    signal,
  });
}

/** Use visits.completeVisit, which retries it. */
export function postComplete(visitId: string, body: CompleteVisit, signal?: AbortSignal): Promise<Visit> {
  return apiRequest(`/visits/${encodeURIComponent(visitId)}/complete`, { method: "POST", body, signal });
}

/** 404 `transcript_not_found` until the visit is transcribed. */
export function getTranscript(visitId: string, signal?: AbortSignal): Promise<Transcript> {
  return apiRequest(`/visits/${encodeURIComponent(visitId)}/transcript`, { signal });
}

/** Everything the review page shows (batch 14). */
export function getReview(visitId: string, signal?: AbortSignal): Promise<Review> {
  return apiRequest(`/visits/${encodeURIComponent(visitId)}/review`, { signal });
}

/** A category or severity change re-routes it; the answer has its new teams. */
export function patchInsight(insightId: string, change: InsightChange, signal?: AbortSignal): Promise<Insight> {
  return apiRequest(`/insights/${encodeURIComponent(insightId)}`, { method: "PATCH", body: change, signal });
}

export function deleteInsight(insightId: string, signal?: AbortSignal): Promise<void> {
  return apiRequest(`/insights/${encodeURIComponent(insightId)}`, { method: "DELETE", signal });
}

/** Its ID is the client's: sending it again returns the one added, so it's safe to retry. */
export function addInsight(visitId: string, body: NewInsight, signal?: AbortSignal): Promise<Insight> {
  return apiRequest(`/visits/${encodeURIComponent(visitId)}/insights`, { method: "POST", body, signal });
}

export function addNote(visitId: string, body: NewNote, signal?: AbortSignal): Promise<Note> {
  return apiRequest(`/visits/${encodeURIComponent(visitId)}/notes`, { method: "POST", body, signal });
}

/** Use review.confirmReview, which retries it: confirming again returns the same visit. */
export function postConfirm(visitId: string, signal?: AbortSignal): Promise<Visit> {
  return apiRequest(`/visits/${encodeURIComponent(visitId)}/confirm`, { method: "POST", signal });
}

/** The visit's audio, for an <audio> element: the browser fetches it, seeking with Range. */
export function audioUrl(visitId: string): string {
  return `${BASE_URL}/visits/${encodeURIComponent(visitId)}/audio`;
}

const NOT_JSON = Symbol("not JSON");

function parseJson(text: string): unknown {
  if (text === "") return NOT_JSON;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return NOT_JSON;
  }
}

/** Keeps whatever the error body gets right, and falls back field by field (§9.6). */
function errorFrom(status: number, body: unknown): ApiError {
  const fallback = `The server answered with an error (HTTP ${status}).`;
  const kept = body === NOT_JSON ? undefined : body;
  const error = isRecord(body) && isRecord(body.error) ? body.error : undefined;
  if (error === undefined) return new ApiError(status, UNEXPECTED_RESPONSE, fallback, undefined, kept);
  return new ApiError(
    status,
    typeof error.code === "string" && error.code !== "" ? error.code : UNEXPECTED_RESPONSE,
    typeof error.message === "string" && error.message !== "" ? error.message : fallback,
    isRecord(error.details) ? error.details : undefined,
    kept,
  );
}

function isHealth(value: unknown): value is Health {
  const states = ["ok", "error"];
  return isRecord(value) && states.includes(value.status as string) && states.includes(value.database as string);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
