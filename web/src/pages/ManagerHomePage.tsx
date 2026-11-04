import { Fragment, useState } from "react";
import { Link } from "react-router";
import { BriefsSection } from "../components/BriefsSection";
import { RetryAlert } from "../components/RetryAlert";
import {
  EMPTY,
  Figure,
  Greeting,
  H2,
  ISSUE_PILLS,
  PILL,
  RECORD,
  SECONDARY,
  SHOWN_FIRST,
  STAT,
  SUMMARY_STATUS,
  ServerStatus,
  ViewAll,
  stat,
} from "../components/home";
import { getOverview, getSummaryPage, listTriage } from "../lib/api";
import { countOf, formatDay, formatPeriod } from "../lib/format";
import { usePolling } from "../lib/hooks";
import { issueLink, STATUS_LABELS } from "../lib/issues";
import { keepsCatalog, useMe } from "../lib/session";
import type { CauseNumbers, Finding, IssueStatus } from "../types/api";

// A row of a list that is one link: the visits list's look on the rep's home.
const ROW =
  "flex min-h-11 items-center gap-3 px-4 py-3 transition-colors hover:bg-paper focus-visible:outline-2" +
  " focus-visible:-outline-offset-2 motion-reduce:transition-none";
const LIST = "flex flex-col divide-y divide-line overflow-hidden rounded-lg border border-line bg-card";

// What "open" means here: a team still has it.
const ACTIVE: IssueStatus[] = ["open", "in_progress"];


/** `/` for a manager or admin (batch F4): the whole org at a glance. Ask a question first, the
 * org's numbers each in plain words, then what waits in triage, last week's top open issues as
 * its summary ranks them, and that summary. An admin adds the catalog's names to review. Never a
 * rep's ranking or comparison: visits are counted for the org as a whole. */
export function ManagerHomePage() {
  const me = useMe();
  const overview = usePolling((signal) => getOverview(signal), { key: "overview", shouldPoll: () => false });
  const triage = usePolling((signal) => listTriage(signal), { key: "triage", shouldPoll: () => false });
  const page = usePolling((signal) => getSummaryPage("org", null, "week", signal), {
    key: "org-week",
    shouldPoll: () => false,
  });
  // Each list's View all (N), opened inline.
  const [allTriage, setAllTriage] = useState(false);
  const [allIssues, setAllIssues] = useState(false);

  const numbers = overview.data;
  // The server sends them newest first.
  const waiting = triage.data ?? [];
  const counted = page.data?.numbers;
  // The org's causes as its summary ranks them, but for those no team still has.
  const causes =
    counted !== undefined && "causes" in counted
      ? counted.causes.filter((cause) => cause.teams.some((work) => ACTIVE.includes(work.status)))
      : [];
  const summary = page.data?.summary ?? null;
  const loading = [overview, triage, page].some((loaded) => loaded.data === undefined && loaded.error === null);

  const inTriage = stat(numbers?.triage ?? undefined, {
    one: "finding waiting in triage for a team",
    many: "findings waiting in triage for a team",
    none: "Nothing waiting in triage",
    loading: "Findings waiting in triage",
  });
  const open = stat(numbers === undefined ? undefined : numbers.open + numbers.in_progress, {
    one: "issue open across your teams",
    many: "issues open across your teams",
    none: "No open issues",
    loading: "Issues open across your teams",
  });
  const resolved = stat(numbers?.resolved_this_week, {
    one: "issue resolved this week",
    many: "issues resolved this week",
    none: "No issues resolved yet this week",
    loading: "Issues resolved this week",
  });
  const visits = stat(numbers?.visits_this_week ?? undefined, {
    one: "visit by your reps this week",
    many: "visits by your reps this week",
    none: "No visits yet this week",
    loading: "Visits by your reps this week",
  });
  const names = stat(numbers?.names_to_review ?? undefined, {
    one: "unknown name to review in the catalog",
    many: "unknown names to review in the catalog",
    none: "No unknown names to review",
    loading: "Unknown names to review in the catalog",
  });

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-8 px-4 py-6 sm:max-w-3xl sm:py-8">
      <Greeting name={me.name} org={me.org_name}>
        What's happening across your teams this week.
      </Greeting>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-6">
        {/* The Ask box is at the top of the inbox. */}
        <Link to="/inbox" className={RECORD}>
          <span
            aria-hidden="true"
            className="grid size-10 shrink-0 place-items-center rounded-full border-[1.5px] border-ink bg-card"
          >
            <svg
              viewBox="0 0 24 24"
              focusable="false"
              className="size-5"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" />
            </svg>
          </span>
          Ask a question
          <span aria-hidden="true" className="ml-auto">
            →
          </span>
        </Link>
        <p className="text-sm text-ink-2">
          Across every visit, in Hindi, Marathi or English: "Lay's pricing complaints in Old Market this month".
        </p>
      </div>

      <ul aria-label="Your numbers" className="grid gap-3 sm:grid-cols-2">
        <li>
          <Link to="/inbox/triage" className={STAT}>
            <Figure {...inTriage} />
          </Link>
        </li>
        <li>
          <Link to="/inbox" className={STAT}>
            <Figure {...open} />
          </Link>
        </li>
        <li>
          <Link to="/inbox?status=resolved" className={STAT}>
            <Figure {...resolved} />
          </Link>
        </li>
        <li>
          <Link to="/visits" className={STAT}>
            <Figure {...visits} />
          </Link>
        </li>
        {keepsCatalog(me) && (
          // The fifth card takes the row: no gap beside it.
          <li className="sm:col-span-2">
            <Link to="/catalog" className={STAT}>
              <Figure {...names} />
            </Link>
          </li>
        )}
      </ul>

      {overview.error !== null && (
        <RetryAlert onRetry={overview.reload}>Your numbers couldn't be loaded: {overview.error.message}</RetryAlert>
      )}
      {triage.error !== null && (
        <RetryAlert onRetry={triage.reload}>Triage couldn't be loaded: {triage.error.message}</RetryAlert>
      )}
      {page.error !== null && (
        <RetryAlert onRetry={page.reload}>Last week's summary couldn't be loaded: {page.error.message}</RetryAlert>
      )}
      {loading && (
        <p role="status" className="text-ink-2">
          Loading…
        </p>
      )}

      {/* On a wide screen: triage on the left; last week's top issues, and its summary, beside it. */}
      <div className="grid gap-8 lg:grid-cols-2 lg:items-start">
        <section aria-labelledby="triage-heading" className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <h2 id="triage-heading" className={H2}>
              Waiting in triage
            </h2>
            {waiting.length > 0 && (
              <p className="text-sm text-ink-2">Findings with no team yet. Give each one a team so it reaches an inbox.</p>
            )}
          </div>
          {triage.data !== undefined && waiting.length === 0 && (
            <p className={EMPTY}>Nothing waiting: every finding has a team.</p>
          )}
          {waiting.length > 0 && (
            <ul id="triage-list" className={LIST}>
              {(allTriage ? waiting : waiting.slice(0, SHOWN_FIRST)).map((finding) => (
                <TriageRow key={finding.insight.id} finding={finding} />
              ))}
            </ul>
          )}
          <ViewAll count={waiting.length} open={allTriage} controls="triage-list" onToggle={setAllTriage} />
        </section>

        <div className="flex flex-col gap-8">
          <section aria-labelledby="issues-heading" className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <h2 id="issues-heading" className={H2}>
                Top open issues
              </h2>
              {causes.length > 0 && (
                <p className="text-sm text-ink-2">
                  As last week's summary ranks them: how serious, how many shops wait on a fix, and for how long. Each team's status is as the week ended.
                </p>
              )}
            </div>
            {page.data !== undefined && causes.length === 0 && <p className={EMPTY}>No open issues at the end of last week.</p>}
            {causes.length > 0 && (
              <ul id="issues-list" className={LIST}>
                {(allIssues ? causes : causes.slice(0, SHOWN_FIRST)).map((cause) => (
                  <CauseRow key={cause.issue_key} cause={cause} />
                ))}
              </ul>
            )}
            <ViewAll count={causes.length} open={allIssues} controls="issues-list" onToggle={setAllIssues} />
          </section>

          <section aria-labelledby="summary-heading" className="flex flex-col gap-3">
            <h2 id="summary-heading" className={H2}>
              Latest summary
            </h2>
            {page.data !== undefined && summary === null && (
              <p className={EMPTY}>No summary yet for last week. It's written once the week ends.</p>
            )}
            {page.data !== undefined && summary !== null && (
              <div className="flex flex-col items-start gap-3 rounded-lg border border-line bg-card p-4">
                <p className="flex flex-col gap-0.5">
                  <span className="font-semibold text-ink">Last week's summary</span>{" "}
                  <span className="text-sm text-ink-3">
                    {formatPeriod("week", page.data.period_start, page.data.period_end)}
                  </span>
                </p>
                <span className={`${PILL} ${SUMMARY_STATUS[summary.status].tone}`}>
                  {SUMMARY_STATUS[summary.status].label}
                </span>
                <Link to="/summaries?scope=org" className={SECONDARY}>
                  Read it →
                </Link>
              </div>
            )}
          </section>

          <BriefsSection />
        </div>
      </div>

      <ServerStatus />
    </main>
  );
}

/** A finding with no team: where it was said, what, and when; Triage gives it one. */
function TriageRow({ finding }: { finding: Finding }) {
  return (
    <li>
      <Link to="/inbox/triage" className={ROW}>
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="font-semibold wrap-break-word text-ink">
            {finding.shop_name}
            <span className="font-normal text-ink-2">, {finding.area}</span>
          </span>{" "}
          <span className="text-sm wrap-break-word text-ink-2">{finding.insight.summary_en}</span>{" "}
          <span className="text-sm text-ink-3">{formatDay(finding.visited_at)}</span>
        </span>
        <span aria-hidden="true" className="shrink-0 text-ink-3">
          →
        </span>
      </Link>
    </li>
  );
}

/** One of last week's causes: its title, each team working it with its status, and how many
 * shops still wait on a fix. It opens the issue (batch F7): the first team's still working it,
 * in that team's inbox. */
function CauseRow({ cause }: { cause: CauseNumbers }) {
  const work = cause.teams.find((team) => team.status === "open" || team.status === "in_progress") ?? cause.teams[0];
  const to =
    work === undefined
      ? "/summaries?scope=org"
      : issueLink(work.issue_id, { team: work.team_key, resolved: work.status === "resolved" });
  return (
    <li>
      <Link to={to} className={ROW}>
        <span className="flex min-w-0 flex-1 flex-col gap-1.5">
          <span className="font-semibold wrap-break-word text-ink">{cause.title_en}</span>{" "}
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {cause.teams.map((work) => (
              <Fragment key={work.issue_id}>
                <span className={`${PILL} ${ISSUE_PILLS[work.status]}`}>
                  {work.team_name}: {STATUS_LABELS[work.status]}
                </span>{" "}
              </Fragment>
            ))}
          </span>{" "}
          <span className="text-sm text-ink-3">{countOf(cause.measures.open_shops, "shop", "shops")} waiting on a fix</span>
        </span>
        <span aria-hidden="true" className="shrink-0 text-ink-3">
          →
        </span>
      </Link>
    </li>
  );
}
