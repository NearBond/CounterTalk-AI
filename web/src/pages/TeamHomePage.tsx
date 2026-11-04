import { useState } from "react";
import { Link } from "react-router";
import { RetryAlert } from "../components/RetryAlert";
import {
  EMPTY,
  FOCUS,
  Figure,
  Greeting,
  H2,
  PILL,
  QUIET_LINK,
  RECORD,
  SHOWN_FIRST,
  STAT,
  SUMMARY_STATUS,
  ServerStatus,
  ViewAll,
  stat,
} from "../components/home";
import { getOverview, getSummaryPage, listIssues } from "../lib/api";
import { countOf, formatDay, formatPeriod } from "../lib/format";
import { usePolling } from "../lib/hooks";
import { issueLink, STATUS_LABELS } from "../lib/issues";
import { useMe } from "../lib/session";
import type { Issue, SummaryPage } from "../types/api";

// The overdue card, tinted while anything is past its due date.
const ALARM = STAT.replace("border-line bg-card", "border-vermilion bg-vermilion-soft");
// An issue's row: a card that is one link to the inbox, where the issue expands.
const ROW =
  "flex flex-col gap-1.5 rounded-lg border border-line bg-card p-4 transition hover:border-ink" +
  ` hover:shadow-[3px_3px_0_var(--color-ink)] motion-reduce:transition-none ${FOCUS}`;

const SEVERITY: Record<string, { label: string; tone: string }> = {
  high: { label: "High severity", tone: "bg-vermilion-soft text-vermilion-ink" },
  medium: { label: "Medium severity", tone: "bg-marigold-soft text-ink" },
  low: { label: "Low severity", tone: "bg-paper-2 text-ink-2" },
};


/** `/` for a team member (batch F4): their team's numbers each in plain words, one big Open the
 * inbox, what needs action in the inbox's own order, what was resolved and what the shop was
 * told, and the team's latest summary. Only their own team's issues. */
export function TeamHomePage() {
  const me = useMe();
  const teamId = me.team?.id ?? null;
  const overview = usePolling((signal) => getOverview(signal), { key: "overview", shouldPoll: () => false });
  // Open and in progress, highest severity first, then most shops, then the most recent.
  const active = usePolling((signal) => listIssues(null, {}, signal), { key: "active", shouldPoll: () => false });
  const resolved = usePolling((signal) => listIssues(null, { status: "resolved" }, signal), {
    key: "resolved",
    shouldPoll: () => false,
  });
  const summary = usePolling((signal) => getSummaryPage("team", teamId, "week", signal), {
    key: `summary:${teamId}`,
    shouldPoll: () => false,
    skip: teamId === null,
  });
  // Each list's View all (N), opened inline.
  const [allActive, setAllActive] = useState(false);
  const [allResolved, setAllResolved] = useState(false);
  const needsAction = active.data ?? [];
  const done = newestFirst(resolved.data ?? []);
  const counts = overview.data;

  const open = stat(counts?.open, {
    one: "open issue waiting for your team",
    many: "open issues waiting for your team",
    none: "No open issues",
    loading: "Open issues waiting for your team",
  });
  const working = stat(counts?.in_progress, {
    one: "issue your team is working on",
    many: "issues your team is working on",
    none: "No issues being worked on",
    loading: "Issues your team is working on",
  });
  const overdue = stat(counts?.overdue, {
    one: "issue past its due date",
    many: "issues past their due date",
    none: "Nothing overdue",
    loading: "Issues past their due date",
  });
  const fixed = stat(counts?.resolved_this_week, {
    one: "issue resolved this week",
    many: "issues resolved this week",
    none: "Nothing resolved yet this week",
    loading: "Issues resolved this week",
  });

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-8 px-4 py-6 sm:max-w-3xl sm:py-8">
      <Greeting name={me.name} org={me.org_name}>
        {me.team === null ? "Your team's issues at a glance." : `Your ${me.team.name} team's issues at a glance.`}
      </Greeting>

      <div className="flex flex-col gap-3">
        <ul aria-label="Your numbers" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <li>
            <Link to="/inbox?status=open" className={STAT}>
              <Figure {...open} />
            </Link>
          </li>
          <li>
            <Link to="/inbox?status=in_progress" className={STAT}>
              <Figure {...working} />
            </Link>
          </li>
          <li>
            <Link to="/inbox" className={(counts?.overdue ?? 0) > 0 ? ALARM : STAT}>
              <Figure {...overdue} />
            </Link>
          </li>
          <li>
            <Link to="/inbox?status=resolved" className={STAT}>
              <Figure {...fixed} />
            </Link>
          </li>
        </ul>
        {overview.error !== null && (
          <RetryAlert onRetry={overview.reload}>Your team's numbers couldn't be loaded: {overview.error.message}</RetryAlert>
        )}
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-6">
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
              <path d="M22 12h-6l-2 3h-4l-2-3H2" />
              <path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z" />
            </svg>
          </span>
          Open the inbox
          <span aria-hidden="true" className="ml-auto">
            →
          </span>
        </Link>
        <p className="text-sm text-ink-2">Assign, resolve and answer what shops raised.</p>
      </div>

      {/* On a wide screen: what needs action on the left; what was resolved, and the summary,
          beside it. */}
      <div className="grid gap-8 lg:grid-cols-2 lg:items-start">
        <section aria-labelledby="needs-action" className="flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <h2 id="needs-action" className={H2}>
              Needs action
            </h2>
            <p className="text-sm text-ink-2">Your team's open issues and the ones in progress, the most pressing first.</p>
          </div>
          {active.error !== null && (
            <RetryAlert onRetry={active.reload}>The issues couldn't be loaded: {active.error.message}</RetryAlert>
          )}
          {active.data === undefined && active.error === null && (
            <p role="status" className="text-ink-2">
              Loading…
            </p>
          )}
          {active.data !== undefined && needsAction.length === 0 && (
            <p className={EMPTY}>Nothing needs action: your team is all caught up.</p>
          )}
          {needsAction.length > 0 && (
            <ul id="needs-action-list" className="flex flex-col gap-2">
              {(allActive ? needsAction : needsAction.slice(0, SHOWN_FIRST)).map((issue) => (
                <ActionRow key={issue.id} issue={issue} />
              ))}
            </ul>
          )}
          <ViewAll count={needsAction.length} open={allActive} controls="needs-action-list" onToggle={setAllActive} />
        </section>

        <div className="flex flex-col gap-8">
          <section aria-labelledby="resolved-heading" className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <h2 id="resolved-heading" className={H2}>
                Recently resolved
              </h2>
              {done.length > 0 && (
                <p className="text-sm text-ink-2">What your team resolved, the latest first, and what each shop was told.</p>
              )}
            </div>
            {resolved.error !== null && (
              <RetryAlert onRetry={resolved.reload}>
                The resolved issues couldn't be loaded: {resolved.error.message}
              </RetryAlert>
            )}
            {resolved.data !== undefined && done.length === 0 && (
              <p className={EMPTY}>
                Nothing resolved yet. When your team resolves an issue, it shows here with what the shop was told.
              </p>
            )}
            {done.length > 0 && (
              <ul id="resolved-list" className="flex flex-col gap-2">
                {(allResolved ? done : done.slice(0, SHOWN_FIRST)).map((issue) => (
                  <ResolvedRow key={issue.id} issue={issue} />
                ))}
              </ul>
            )}
            <ViewAll count={done.length} open={allResolved} controls="resolved-list" onToggle={setAllResolved} />
          </section>

          <section aria-labelledby="summary-heading" className="flex flex-col gap-3">
            <h2 id="summary-heading" className={H2}>
              Your team's latest summary
            </h2>
            {summary.error !== null && (
              <RetryAlert onRetry={summary.reload}>The summary couldn't be loaded: {summary.error.message}</RetryAlert>
            )}
            {summary.data !== undefined && <LatestSummary page={summary.data} />}
            {teamId === null && (
              <p className={EMPTY}>You're not on a team yet, so there's no team summary to show.</p>
            )}
          </section>
        </div>
      </div>

      <ServerStatus />
    </main>
  );
}

/** The resolved issues, the latest resolved first. */
function newestFirst(issues: Issue[]): Issue[] {
  const at = (issue: Issue) => (issue.resolved_at === null ? 0 : new Date(issue.resolved_at).getTime() || 0);
  return [...issues].sort((a, b) => at(b) - at(a));
}

/** An issue to work: what it is, how bad, where it stands, how many shops raised it, and whose
 * it is. */
function ActionRow({ issue }: { issue: Issue }) {
  const severity = SEVERITY[issue.severity] ?? { label: `${issue.severity} severity`, tone: "bg-paper-2 text-ink-2" };
  return (
    <li>
      <Link to={issueLink(issue.id)} className={ROW}>
        <span className="font-semibold wrap-break-word text-ink">{issue.title_en}</span>{" "}
        <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className={`${PILL} ${severity.tone}`}>{severity.label}</span>{" "}
          <span className={`${PILL} text-ink-2 ring-1 ring-line-strong ring-inset`}>{STATUS_LABELS[issue.status]}</span>
          {issue.overdue && (
            <>
              {" "}
              <span className={`${PILL} bg-vermilion-ink text-card`}>Overdue</span>
            </>
          )}
          {issue.raised_again && (
            <>
              {" "}
              <span className={`${PILL} bg-paper-2 text-ink-2`}>Raised again</span>
            </>
          )}
        </span>{" "}
        <span className="text-sm wrap-break-word text-ink-3">
          {countOf(issue.shops, "shop", "shops")} · {countOf(issue.insights, "finding", "findings")} ·{" "}
          {issue.owner_name === null ? "No owner yet" : `Owner: ${issue.owner_name}`}
        </span>
      </Link>
    </li>
  );
}

/** A resolved issue: what it was, the day it was resolved, and the line the shop was told. */
function ResolvedRow({ issue }: { issue: Issue }) {
  return (
    <li>
      <Link to={issueLink(issue.id, { resolved: true })} className={ROW}>
        <span className="font-semibold wrap-break-word text-ink">{issue.title_en}</span>
        {issue.resolved_at !== null && (
          <>
            {" "}
            <span className="text-sm text-ink-3">resolved {formatDay(issue.resolved_at)}</span>
          </>
        )}
        {issue.resolution_note !== null && (
          <>
            {" "}
            <span className="border-l-4 border-leaf pl-3 leading-snug wrap-break-word text-ink">
              <span className="text-sm font-bold tracking-wide text-leaf uppercase">Told the shop:</span>{" "}
              {issue.resolution_note}
            </span>
          </>
        )}
      </Link>
    </li>
  );
}

/** Last week's summary: its week, whether it can be read yet, and the way to it. None asked for
 * yet: what one is, and where to ask. */
function LatestSummary({ page }: { page: SummaryPage }) {
  const period = formatPeriod("week", page.period_start, page.period_end);
  const { summary } = page;
  const link = `${QUIET_LINK} self-start font-semibold text-pen`;
  if (summary === null) {
    return (
      <>
        <p className={EMPTY}>
          No summary of last week ({period}) yet. On Summaries, Write the summary turns the week's findings into a short
          read for your team.
        </p>
        <Link to="/summaries" className={link}>
          Go to Summaries →
        </Link>
      </>
    );
  }
  const status = SUMMARY_STATUS[summary.status];
  const { insights, shops } = page.numbers.totals;
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-line bg-card p-4">
      <h3 className="text-lg leading-snug font-semibold text-ink">Last week's summary</h3>
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-ink-3">
        <span>{period}</span> <span className={`${PILL} ${status.tone}`}>{status.label}</span>
      </p>
      <p className="text-sm text-ink-2">
        {insights === 0
          ? "No findings last week."
          : `${countOf(insights, "finding", "findings")} from ${countOf(shops, "shop", "shops")} last week.`}
      </p>
      <Link to="/summaries" className={link}>
        {summary.status === "failed" ? "Try again on Summaries →" : "Read it →"}
      </Link>
    </div>
  );
}
