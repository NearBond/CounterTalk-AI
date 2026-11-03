/**
 * The only place that calls fetch (CODING_GUIDELINES §4.3). Every failure becomes an ApiError,
 * so a caller has one thing to catch and always gets a status, a code and a readable message.
 */
import type {
  AskAnswer,
  CatalogItem,
  CatalogItemChange,
  CatalogKind,
  CompleteVisit,
  ConsentAnswer,
  ConsentLanguage,
  ConsentLink,
  ConsentPage,
  Drill,
  FixedItem,
  ErrorBody,
  FrameworkErrorCode,
  Health,
  InboxOptions,
  Me,
  MyCounts,
  NameReview,
  NewAlias,
  NewCatalogItem,
  Insight,
  InsightChange,
  IssueChange,
  IssueDetail,
  IssueStatus,
  Issue,
  MergeIn,
  NewInsight,
  NewNote,
  NewVisit,
  Note,
  Page,
  PaperConsent,
  PeriodKind,
  Resolved,
  ResolveName,
  Review,
  RouteSeen,
  RouteTo,
  Segment,
  Shop,
  SignIn,
  Summary,
  SummaryAsk,
  SummaryPage,
  SummaryScope,
  Transcript,
  Finding,
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

/** The session ended (401 signed_out): set by the signed-in layout, which goes to /signin. */
let onSignedOut: (() => void) | null = null;

export function whenSignedOut(handler: (() => void) | null): void {
  onSignedOut = handler;
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
  if (!response.ok) {
    const error = errorFrom(response.status, body);
    if (error.status === 401 && error.code === "signed_out") onSignedOut?.();
    throw error;
  }
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

// --- signing in (batch 16). The session is an HttpOnly cookie: the app never sees it. -----

/** 401 wrong_email_or_password, or 429 too_many_tries (its message says when to try again). */
export function signIn(body: SignIn, signal?: AbortSignal): Promise<Me> {
  return apiRequest("/auth/signin", { method: "POST", body, signal });
}

export function signOut(signal?: AbortSignal): Promise<void> {
  return apiRequest("/auth/signout", { method: "POST", signal });
}

/** 401 signed_out without a session. */
export function getMe(signal?: AbortSignal): Promise<Me> {
  return apiRequest("/auth/me", { signal });
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

// --- the inbox (batch 15). Its changes name the version they read, so none is retried
// silently: a lost answer's retry would be refused as out of date (§9.11). ---------------------

/** The filters an inbox list takes: none is "open and in progress", of any of the rest. */
export interface IssueFilters {
  status?: Exclude<IssueStatus, "merged">;
  category?: string;
  area?: string;
  distributor?: string;
  /** Batch 17c: catalog item IDs, and IST days (YYYY-MM-DD), both in the period. */
  competitor?: string;
  product?: string;
  from?: string;
  to?: string;
}

export function getInboxOptions(signal?: AbortSignal): Promise<InboxOptions> {
  return apiRequest("/issues/options", { signal });
}

/** A team's issues. `team` is its key: a manager's or admin's pick; null for a team member,
 * whose team is their own. */
export function listIssues(team: string | null, filters: IssueFilters, signal?: AbortSignal): Promise<Issue[]> {
  const query = new URLSearchParams(team === null ? {} : { team });
  for (const [name, value] of Object.entries(filters)) {
    if (value) query.set(name, value);
  }
  const asked = query.toString();
  return apiRequest(asked ? `/issues?${asked}` : "/issues", { signal });
}

export function getIssue(issueId: string, signal?: AbortSignal): Promise<IssueDetail> {
  return apiRequest(`/issues/${encodeURIComponent(issueId)}`, { signal });
}

export function changeIssue(issueId: string, change: IssueChange, signal?: AbortSignal): Promise<IssueDetail> {
  return apiRequest(`/issues/${encodeURIComponent(issueId)}`, { method: "PATCH", body: change, signal });
}

export function listTriage(signal?: AbortSignal): Promise<Finding[]> {
  return apiRequest("/issues/triage", { signal });
}

export function acceptRoute(insightId: string, seen: RouteSeen, signal?: AbortSignal): Promise<Insight> {
  return apiRequest(`/insights/${encodeURIComponent(insightId)}/accept`, { method: "POST", body: seen, signal });
}

export function moveRoute(insightId: string, to: RouteTo, signal?: AbortSignal): Promise<Insight> {
  return apiRequest(`/insights/${encodeURIComponent(insightId)}/move`, { method: "POST", body: to, signal });
}

export function alsoSend(insightId: string, to: RouteTo, signal?: AbortSignal): Promise<Insight> {
  return apiRequest(`/insights/${encodeURIComponent(insightId)}/also-send`, { method: "POST", body: to, signal });
}

/** From triage: 409 `already_routed` if someone gave it a team meanwhile. */
export function assignTeam(insightId: string, teamId: string, signal?: AbortSignal): Promise<Insight> {
  return apiRequest(`/insights/${encodeURIComponent(insightId)}/team`, {
    method: "POST",
    body: { team_id: teamId },
    signal,
  });
}

/** Merge, as suggested (batch 15c): the kept issue, read again. Merged already: the same answer. */
export function mergeSuggestion(suggestionId: string, body: MergeIn, signal?: AbortSignal): Promise<IssueDetail> {
  return apiRequest(`/issues/suggestions/${encodeURIComponent(suggestionId)}/merge`, {
    method: "POST",
    body,
    signal,
  });
}

/** Dismiss: the two stay apart, and the pair is never suggested again. */
export function dismissSuggestion(suggestionId: string, signal?: AbortSignal): Promise<void> {
  return apiRequest(`/issues/suggestions/${encodeURIComponent(suggestionId)}/dismiss`, {
    method: "POST",
    signal,
  });
}

/** A summary's page: the period's numbers at once, and its summary if one was asked for. */
export function getSummaryPage(
  scope: SummaryScope,
  teamId: string | null,
  period: PeriodKind,
  signal?: AbortSignal,
): Promise<SummaryPage> {
  const query = new URLSearchParams({ scope, period });
  if (teamId !== null) query.set("team_id", teamId);
  return apiRequest(`/summaries?${query.toString()}`, { signal });
}

/** A team summary's issue drilled down to each quote (batch 15c), counted now. */
export function getDrill(
  teamId: string,
  issueId: string,
  period: PeriodKind,
  start: string | null,
  signal?: AbortSignal,
): Promise<Drill> {
  const query = new URLSearchParams({ team_id: teamId, issue_id: issueId, period });
  if (start !== null) query.set("start", start); // the period the summary page showed
  return apiRequest(`/summaries/drill?${query.toString()}`, { signal });
}

/** Asks for it: made, with its job queued, only the first time. */
export function askSummary(body: SummaryAsk, signal?: AbortSignal): Promise<Summary> {
  return apiRequest("/summaries", { method: "POST", body, signal });
}

/** One summary, polled while it's written: nothing is counted. */
export function getSummary(summaryId: string, signal?: AbortSignal): Promise<Summary> {
  return apiRequest(`/summaries/${encodeURIComponent(summaryId)}`, { signal });
}

/** Try again: a failed summary is written again; any other is left as it is. */
export function retrySummary(summaryId: string, signal?: AbortSignal): Promise<Summary> {
  return apiRequest(`/summaries/${encodeURIComponent(summaryId)}/retry`, { method: "POST", signal });
}

/** The visit's audio, for an <audio> element: the browser fetches it, seeking with Range. */
export function audioUrl(visitId: string): string {
  return `${BASE_URL}/visits/${encodeURIComponent(visitId)}/audio`;
}

// --- ask a question (batch 17d) -------------------------------------------------------------

/** A question across visits: never retried here, as each one counts against the day's. */
export function askQuestion(question: string, signal?: AbortSignal): Promise<AskAnswer> {
  return apiRequest("/ask", { method: "POST", body: { question }, signal });
}

// --- the shop pick (batch 17b) --------------------------------------------------------------

/** What got fixed for the shop since its last visit before today, newest first. */
export function getFixed(shopId: string, signal?: AbortSignal): Promise<Page<FixedItem>> {
  return apiRequest(`/shops/${encodeURIComponent(shopId)}/fixed`, { signal });
}

/** The signed-in rep's own counts this month: never anyone else's. */
export function getMyCounts(signal?: AbortSignal): Promise<MyCounts> {
  return apiRequest("/me/counts", { signal });
}

// --- consent before the visit (batch 17) ---------------------------------------------------

/** A new link to the shop's consent page, to share; the ones shared before stay valid. */
export function shareConsentLink(shopId: string, signal?: AbortSignal): Promise<ConsentLink> {
  return apiRequest(`/shops/${encodeURIComponent(shopId)}/consent-links`, { method: "POST", signal });
}

/** The shopkeeper signed the paper form in this language: a yes, marked by the signed-in user.
 * 409 consent_refused if the shop said no on its page. */
export function markPaperConsent(
  shopId: string,
  language: ConsentLanguage,
  signal?: AbortSignal,
): Promise<PaperConsent> {
  return apiRequest(`/shops/${encodeURIComponent(shopId)}/consent`, {
    method: "POST",
    body: { language },
    signal,
  });
}

/** A shop's public consent page, by its link's token: 404 for any token that isn't one. */
export function getConsentPage(token: string, signal?: AbortSignal): Promise<ConsentPage> {
  return apiRequest(`/consent/${encodeURIComponent(token)}`, { signal });
}

export function answerConsent(token: string, body: ConsentAnswer, signal?: AbortSignal): Promise<ConsentPage> {
  return apiRequest(`/consent/${encodeURIComponent(token)}`, { method: "POST", body, signal });
}

/** Whether a recording that wouldn't load was deleted under the retention policy (batch 17):
 * an <audio> element never sees the server's answer, so the player asks once. */
export async function recordingDeleted(url: string): Promise<boolean> {
  try {
    const response = await fetch(url, { headers: { Range: "bytes=0-0" } });
    if (response.status !== 404) return false;
    const body = parseJson(await response.text());
    return isRecord(body) && isRecord(body.error) && body.error.code === "audio_deleted";
  } catch {
    return false; // no answer: not known to be deleted, so Try again stays
  }
}

// --- the catalog screen (batch 16b): admins only. Its edits name the version they read. ------

export function listCatalog(q: string, kind: CatalogKind | "", signal?: AbortSignal): Promise<Page<CatalogItem>> {
  const query = new URLSearchParams();
  if (q.trim()) query.set("q", q.trim());
  if (kind) query.set("kind", kind);
  const asked = query.toString();
  return apiRequest(asked ? `/catalog/items?${asked}` : "/catalog/items", { signal });
}

/** 409 item_exists, or name_in_use for a name another item is matched on. */
export function addCatalogItem(body: NewCatalogItem, signal?: AbortSignal): Promise<CatalogItem> {
  return apiRequest("/catalog/items", { method: "POST", body, signal });
}

/** 409 item_changed if someone changed it since it was read. */
export function changeCatalogItem(itemId: string, change: CatalogItemChange, signal?: AbortSignal): Promise<CatalogItem> {
  return apiRequest(`/catalog/items/${encodeURIComponent(itemId)}`, { method: "PATCH", body: change, signal });
}

/** The item with its new alias. 400 alias_empty; 409 alias_in_use. */
export function addAlias(itemId: string, body: NewAlias, signal?: AbortSignal): Promise<CatalogItem> {
  return apiRequest(`/catalog/items/${encodeURIComponent(itemId)}/aliases`, { method: "POST", body, signal });
}

export function removeAlias(aliasId: string, signal?: AbortSignal): Promise<void> {
  return apiRequest(`/catalog/aliases/${encodeURIComponent(aliasId)}`, { method: "DELETE", signal });
}

export function listNameReviews(signal?: AbortSignal): Promise<NameReview[]> {
  return apiRequest("/catalog/reviews", { signal });
}

/** The same again changes nothing; 409 review_resolved for another way. */
export function resolveName(reviewId: string, body: ResolveName, signal?: AbortSignal): Promise<Resolved> {
  return apiRequest(`/catalog/reviews/${encodeURIComponent(reviewId)}/resolve`, { method: "POST", body, signal });
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
