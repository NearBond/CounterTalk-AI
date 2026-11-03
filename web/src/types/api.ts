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

/** Who may do what (batch 16): countertalk/access.py. */
export type Role = "rep" | "team_member" | "manager" | "admin";

/** MeOut: the signed-in user (GET /api/auth/me, and sign-in's answer). */
export interface Me {
  id: string;
  name: string;
  email: string;
  role: Role;
  /** A team member's team; null for every other role. */
  team: Team | null;
  org_name: string;
}

/** SignInIn: POST /api/auth/signin. */
export interface SignIn {
  email: string;
  password: string;
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

/** A shop's answer about recording its visits (batch 17): models.py, ConsentDecision. */
export type ConsentDecision = "yes" | "no";

/** The languages of the consent notice (consent/v1). */
export type ConsentLanguage = "hi" | "en" | "mr";

/** GET /api/shops: countertalk/api/shops.py, ShopOut. */
export interface Shop {
  id: string;
  name: string;
  area: string;
  do_not_record: boolean;
  /** Its current answer; null until it gives one. A conversation needs a "yes". */
  consent: ConsentDecision | null;
}

/** POST /api/shops/{id}/consent-links: the shop's consent page, from the app's own origin. */
export interface ConsentLink {
  path: string;
}

/** POST /api/shops/{id}/consent (the paper form): ConsentOut. */
export interface PaperConsent {
  consent: ConsentDecision;
}

/** GET and POST /api/consent/{token}: countertalk/api/consent.py, ConsentPageOut. */
export interface ConsentPage {
  company: string;
  shop: string;
  decision: ConsentDecision | null;
  notice_version: number;
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
  team_id: string;
  team_key: string;
  team_name: string;
  routed_by: "table" | "ai" | "override" | "person";
  /** What a team member's accept, Move or Also send names: 409 `route_changed` if it moved on. */
  version: number;
  /** Its team's issue once the visit is confirmed (batch 15); null for a route confirmed before
   * issues existed: the rep's page shows the team with no status then. */
  issue_id: string | null;
  issue_status: IssueStatus | null;
  /** The answer the rep takes back to the shop, once resolved. */
  resolution_note: string | null;
}

export type IssueStatus = "open" | "in_progress" | "resolved" | "merged";

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
 * `invalid_severity`, `summary_empty`, `summary_too_long`, `not_shopkeeper_turn` (only the
 * shopkeeper's lines); 404 `turn_not_found`; 409 as PATCH.
 */
export interface NewInsight {
  /** Made by the client (a UUID): sending the same one again adds nothing. */
  id: string;
  turn_id: number;
  kind: string;
  category: string;
  severity: string;
  summary_en: string;
}

/** POST /api/visits/{id}/notes: NewNote. 400 `note_empty`, `note_too_long`; 409 as PATCH. */
export interface NewNote {
  /** Made by the client (a UUID): sending the same one again adds nothing. */
  id: string;
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

/** OrderItemOut: what the shop ordered, as said. Read-only: the rep can't edit them yet. */
export interface OrderItem {
  seq: number;
  /** As said, in its own script. */
  name: string;
  quantity: number | null;
  /** As said ("dabbe", "peti"), if one was. */
  unit: string | null;
  /** The catalog item the name links to, if it does. */
  sku: string | null;
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
  order_items: OrderItem[];
  insights: Insight[];
  notes: Note[];
  /** Each speaker label's saved role ("c0:A": "rep"): the add-a-finding picker shows it. */
  speaker_roles: Record<string, string>;
  kinds: Choice[];
  categories: Choice[];
  severities: Choice[];
}

/** The longest note the server takes: countertalk/limits.py, NOTE_MAX_CHARS. */
export const NOTE_MAX_CHARS = 2_000;

/** The longest summary a rep may write: countertalk/limits.py, SUMMARY_MAX_CHARS. */
export const SUMMARY_MAX_CHARS = 500;

// --- the inbox (batch 15) ----------------------------------------------------------------------

/** TeamOut: one of the org's teams. */
export interface Team {
  id: string;
  key: string;
  name: string;
}

/** DistributorOut. */
export interface Distributor {
  id: string;
  name: string;
}

/** GET /api/issues/options: OptionsOut, what the inbox's pickers offer: the teams (the dev
 * dropdown, Move and triage), and the filters' categories, areas and distributors. */
export interface InboxOptions {
  teams: Team[];
  categories: Choice[];
  areas: string[];
  distributors: Distributor[];
}

/**
 * GET /api/issues?team=<key>: IssueOut, a card. Counts are of the issue's insights' shops,
 * insights and reps, never mentions; raised_again: a shop with insights from two or more visits
 * in it. Unknown team: 404 `team_not_found`.
 */
export interface Issue {
  id: string;
  team_key: string;
  category: string;
  title_en: string;
  severity: string;
  status: IssueStatus;
  owner_id: string | null;
  owner_name: string | null;
  due_at: string;
  overdue: boolean;
  first_seen_at: string;
  last_seen_at: string;
  resolved_at: string | null;
  resolution_note: string | null;
  reopened_count: number;
  shops: number;
  insights: number;
  reps: number;
  raised_again: boolean;
  /** What a change names: 409 `issue_changed` if it moved on. */
  version: number;
}

/** One insight of an issue: MemberOut. `route` is its route in this issue's team. */
export interface Member {
  insight: Insight;
  shop_name: string;
  area: string;
  rep_name: string;
  visit_id: string;
  visited_at: string;
  route: Route;
}

/** GET and PATCH /api/issues/{id}: IssueDetailOut. 404 `issue_not_found`. */
export interface IssueDetail {
  issue: Issue;
  members: Member[];
  /** Its open merge suggestions (batch 15c), whose two issues are both still active. */
  suggestions: Suggestion[];
}

/** The other issue of a merge suggestion, as it is now: its version is what Merge names. */
export interface OtherIssue {
  id: string;
  title_en: string;
  version: number;
}

/** An open merge suggestion (batch 15c): "These look like the same issue: <reason>". The issue
 * kept is into_issue_id; the other is merged into it. */
export interface Suggestion {
  id: string;
  into_issue_id: string;
  merge_issue_id: string;
  reason: string;
  other: OtherIssue;
}

/**
 * POST /api/issues/suggestions/{id}/merge: MergeIn, both issues' versions as read. Answers the
 * kept issue's IssueDetail. 409 `issue_changed`, `issue_merged`, `issue_closed`, `issue_empty`,
 * `suggestion_dismissed`; 404 `suggestion_not_found`. POST …/dismiss: 204; 409
 * `suggestion_merged`.
 */
export interface MergeIn {
  into_version: number;
  merge_version: number;
}

/**
 * PATCH /api/issues/{id}: IssueChange. 409 `issue_changed` (an old version), `issue_closed`,
 * `issue_merged`; 400 `note_required`, `note_too_long`, `note_only_when_resolving`,
 * `due_in_past`; 403 `not_owner` (only the owner changes the due date).
 */
export interface IssueChange {
  version: number;
  status?: "open" | "in_progress" | "resolved" | null;
  note?: string | null;
  assign_to_me?: boolean;
  /** ISO 8601 with its time zone. */
  due_at?: string | null;
}

/** GET /api/issues/triage: UntriagedOut, a confirmed insight with no team. */
export interface Untriaged {
  insight: Insight;
  shop_name: string;
  area: string;
  rep_name: string;
  visit_id: string;
  visited_at: string;
}

/**
 * POST /api/insights/{id}/accept: RouteSeen, the route acted on as read. 409 `route_changed`,
 * `nothing_to_accept` (not the AI's suggestion), `insight_not_confirmed`.
 */
export interface RouteSeen {
  slot: number;
  version: number;
}

/**
 * POST /api/insights/{id}/move and /also-send: RouteTo. 409 `route_changed`, `already_routed`,
 * `team_limit` (Also send at two teams), `not_routed`; 404 `team_not_found`.
 */
export interface RouteTo {
  slot: number;
  version: number;
  team_id: string;
}

/** POST /api/insights/{id}/team, from triage: TeamFor. 409 `already_routed` if someone gave it
 * one meanwhile. */
export interface TeamFor {
  team_id: string;
}

/** The longest resolution note: countertalk/limits.py, RESOLUTION_NOTE_MAX_CHARS. */
export const RESOLUTION_NOTE_MAX_CHARS = 2_000;

// --- summaries (batch 15b) ---------------------------------------------------------------------

export type SummaryScope = "team" | "org";
/** Last week, or last month: the latest whole one. */
export type PeriodKind = "week" | "month";
export type TrendLabel = "resolved" | "recurring" | "new" | "growing" | "steady";
export type SummaryStatus = "writing" | "ready" | "failed";

/** summaries.Period: a week from Monday or a calendar month, in IST, as [start, end). */
export interface SummaryPeriod {
  kind: PeriodKind;
  start: string;
  end: string;
}

/** summaries.Measures: an issue's numbers in the period, or a cause's across its teams. */
export interface Measures {
  severity: string;
  shops: number;
  insights: number;
  reps: number;
  visits: number;
  /** Up to the period's end: shops on two or more visits, and shops open since the latest
   * resolve. */
  raised_again: number;
  open_shops: number;
  age_weeks: number;
  overdue: boolean;
  /** The mean of shops per period over the previous four. */
  baseline: number;
  seen_periods: number;
  label: TrendLabel;
  /** Severity weight (high 3, medium 2, low 1) x open shops x (1 + age in weeks). */
  score: number;
  last_seen_at: string;
}

/** summaries.IssueNumbers. `status` is as it was at the period's end. */
export interface IssueNumbers {
  issue_id: string;
  title_en: string;
  category: string;
  status: IssueStatus;
  measures: Measures;
}

/** summaries.Totals: distinct insights, shops, reps and visits; triage is the org's only. */
export interface Totals {
  insights: number;
  shops: number;
  reps: number;
  visits: number;
  triage: number;
}

/** summaries.TeamNumbers: a team's issues, ranked. */
export interface TeamNumbers {
  team_id: string;
  period: SummaryPeriod;
  issues: IssueNumbers[];
  totals: Totals;
}

/** summaries.TeamWork: a team working a cause, and its issue's status at the period's end. */
export interface TeamWork {
  team_key: string;
  team_name: string;
  issue_id: string;
  status: IssueStatus;
}

/** summaries.CauseNumbers: one cause across teams, each insight and shop counted once. */
export interface CauseNumbers {
  issue_key: string;
  title_en: string;
  category: string;
  teams: TeamWork[];
  measures: Measures;
}

/** summaries.CategoryArea */
export interface CategoryArea {
  category: string;
  area: string;
  causes: number;
  shops: number;
}

/** summaries.CompetitorCount: insights in the period naming a competitor, and their shops. */
export interface CompetitorCount {
  competitor: string;
  insights: number;
  shops: number;
}

/** summaries.OrgNumbers: the leadership summary's causes, ranked, and the rest. */
export interface OrgNumbers {
  period: SummaryPeriod;
  causes: CauseNumbers[];
  totals: Totals;
  by_category_area: CategoryArea[];
  competitors: CompetitorCount[];
  /** The teams' issues as they stood at the period's end. */
  issues_at_end: { open: number; in_progress: number; overdue: number };
  resolved_in_period: number;
}

/** narrative.Citation: an insight a sentence cites, and its visit, for the link. */
export interface Citation {
  insight_id: string;
  visit_id: string;
}

/** narrative.Sentence */
export interface Sentence {
  text: string;
  cites: Citation[];
}

/** narrative.IssueWords: an issue's (or cause's) sentences; `id` is its issue_id (a team's) or
 * issue_key (the org's). */
export interface IssueWords {
  id: string;
  sentences: Sentence[];
}

/** narrative.Narrative: what the checker kept, and how many it dropped, by why. */
export interface Narrative {
  overview: Sentence[];
  issues: IssueWords[];
  kept: number;
  dropped: Record<string, number>;
}

/** POST /api/summaries, GET /api/summaries/{id}, POST …/retry: SummaryOut. 404
 * `summary_not_found`. */
export interface Summary {
  id: string;
  scope: SummaryScope;
  team_id: string | null;
  period_start: string;
  period_end: string;
  status: SummaryStatus;
  /** Once it's ready: the numbers its narrative was written from, and when they were counted. */
  numbers: TeamNumbers | OrgNumbers | null;
  counted_at: string | null;
  narrative: Narrative | null;
  model: string | null;
  failure_reason: string | null;
}

/** POST /api/summaries: SummaryAsk. 400 `team_needed`, `team_in_org_summary`; 404
 * `team_not_found`. */
export interface SummaryAsk {
  scope: SummaryScope;
  team_id?: string | null;
  period: PeriodKind;
}

/** GET /api/summaries: SummaryPageOut. The summary's own numbers once it's ready, counted now
 * before then: numbers_as_of says when. */
export interface SummaryPage {
  period: PeriodKind;
  period_start: string;
  period_end: string;
  numbers: TeamNumbers | OrgNumbers;
  numbers_as_of: string;
  summary: Summary | null;
}

/** GET /api/summaries/drill: DrillOut, a team summary's issue drilled down (batch 15c). Its
 * latest run up to the period's end, counted now (numbers_as_of; a summary written earlier keeps
 * its own). shops is its open shops while it's open; in_period is the summary's own counts.
 * 404 `issue_not_found`. */
export interface Drill {
  period: PeriodKind;
  period_start: string;
  period_end: string;
  numbers_as_of: string;
  issue_id: string;
  title_en: string;
  category: string;
  /** As it was at the period's end. */
  status: IssueStatus;
  shops: number;
  insights: number;
  reps: number;
  raised_again: number;
  in_period: Totals;
  by_rep: DrillRep[];
}

/** A rep's shops in the issue, and how many insights: never ranked or compared. */
export interface DrillRep {
  rep_id: string;
  rep_name: string;
  insights: number;
  shops: DrillShop[];
}

export interface DrillShop {
  shop_id: string;
  name: string;
  area: string;
  distributor: string | null;
  visits: DrillVisit[];
}

export interface DrillVisit {
  visit_id: string;
  visited_at: string;
  /** In the summary's period, or before it in the issue's run. */
  in_period: boolean;
  insights: Insight[];
}

// --- the catalog screen (batch 16b): countertalk/api/catalog.py --------------------------------

export type CatalogKind = "own" | "competitor";

/** AliasOut. */
export interface CatalogAlias {
  id: string;
  text: string;
  source: string;
}

/** ItemOut. */
export interface CatalogItem {
  id: string;
  key: string;
  kind: CatalogKind;
  company: string;
  brand: string;
  /** null for a brand as a whole. */
  product: string | null;
  active: boolean;
  /** What an edit names (compare-and-set): 409 item_changed if it moved on. */
  version: number;
  aliases: CatalogAlias[];
}

/** NewItem: the names of a new item (a review's "create" takes these). */
export interface CatalogNames {
  company: string;
  brand: string;
  product?: string | null;
}

/** ItemIn: POST /api/catalog/items. */
export interface NewCatalogItem {
  kind: CatalogKind;
  company: string;
  brand: string;
  product?: string | null;
}

/** ItemChange: PATCH /api/catalog/items/{id}. `product: null` makes it the brand's item. */
export interface CatalogItemChange {
  version: number;
  company?: string;
  brand?: string;
  product?: string | null;
  active?: boolean;
}

/** AliasIn. */
export interface NewAlias {
  text: string;
}

/** NameReviewOut: a name linking couldn't place, and the visits it was said in. */
export interface NameReview {
  id: string;
  kind: CatalogKind;
  text: string;
  visit_ids: string[];
}

/** ResolveIn. */
export interface ResolveName {
  action: "attach" | "create" | "dismiss";
  item_id?: string;
  item?: CatalogNames;
}

/** Resolved: the item the name is now an alias of; null when dismissed, or nothing changed. */
export interface Resolved {
  item: CatalogItem | null;
}

