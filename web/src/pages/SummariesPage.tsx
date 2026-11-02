import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { ChoiceSelect } from "../components/ChoiceSelect";
import { RetryAlert } from "../components/RetryAlert";
import { SaveError } from "../components/SaveError";
import { askSummary, getInboxOptions, getSummary, getSummaryPage, retrySummary } from "../lib/api";
import { countOf, formatDateTime, formatPeriod, istDay } from "../lib/format";
import { usePolling, useSave } from "../lib/hooks";
import { STATUS_LABELS } from "../lib/issues";
import type {
  CauseNumbers,
  IssueNumbers,
  Measures,
  Narrative,
  OrgNumbers,
  PeriodKind,
  Sentence,
  Summary,
  SummaryScope,
  TeamNumbers,
  Totals,
  TrendLabel,
} from "../types/api";

const TREND_LABELS: Record<TrendLabel, string> = {
  growing: "Growing",
  recurring: "Recurring",
  new: "New",
  resolved: "Resolved",
  steady: "Steady",
};

/** `/summaries` (batch 15b): a team's last week, or the org's last week or month. The numbers
 * show at once, counted now; the summary's words come from a job, polled by its ID until it's
 * written, and then its own numbers show, as of when they were counted. The team is the dev
 * dropdown's until batch 16 signs a member in. */
export function SummariesPage() {
  const [params, setParams] = useSearchParams();
  const options = usePolling((signal) => getInboxOptions(signal), { key: "options", shouldPoll: () => false });
  const teams = options.data?.teams;
  const scope: SummaryScope = params.get("scope") === "org" ? "org" : "team";
  const period: PeriodKind = params.get("period") === "month" ? "month" : "week";
  const team = teams === undefined ? null : (teams.find((t) => t.key === params.get("team")) ?? teams[0] ?? null);
  const teamId = scope === "team" ? (team?.id ?? null) : null;
  const pageKey = JSON.stringify([scope, teamId, period]);
  const page = usePolling((signal) => getSummaryPage(scope, teamId, period, signal), {
    key: pageKey,
    shouldPoll: () => false,
    skip: scope === "team" && teamId === null,
  });
  // What Write the summary or Try again returned, for this page: polled from there, so the
  // numbers aren't counted again just to learn its ID. Each one is a new round of polling.
  const [mine, setMine] = useState<{ key: string; summary: Summary; round: number } | null>(null);
  const own = mine?.key === pageKey ? mine : null;
  const asked = own?.summary ?? page.data?.summary ?? null;
  const polled = usePolling((signal) => getSummary(asked?.id ?? "", signal), {
    key: `${asked?.id}:${asked?.status}:${own?.round ?? 0}`,
    shouldPoll: (found) => found.status === "writing",
    skip: asked?.status !== "writing",
  });
  const summary = polled.data ?? asked;
  const ready = summary?.status === "ready" && summary.numbers !== null ? summary : null;
  const numbers = ready?.numbers ?? page.data?.numbers;
  const asOf = ready?.counted_at ?? page.data?.numbers_as_of;
  const save = useSave();
  const keep = (summary: Summary) => setMine({ key: pageKey, summary, round: (own?.round ?? 0) + 1 });
  const ask = () => save.run((signal) => askSummary({ scope, team_id: teamId, period }, signal), keep);
  const retry = (summaryId: string) => save.run((signal) => retrySummary(summaryId, signal), keep);
  const choose = (name: string, value: string) => {
    const next = new URLSearchParams(params);
    next.set(name, value);
    setParams(next, { replace: true });
  };

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
      <div className="flex gap-4">
        <Link to="/" className="text-blue-700 underline">
          Visits
        </Link>
        <Link to="/inbox" className="text-blue-700 underline">
          Inbox
        </Link>
      </div>
      <h1 className="text-2xl font-semibold">Summaries</h1>
      {options.error !== null && (
        <RetryAlert onRetry={options.reload}>The teams couldn't be loaded: {options.error.message}</RetryAlert>
      )}
      {teams !== undefined && (
        <section aria-label="Which summary" className="grid grid-cols-2 gap-2">
          <ChoiceSelect
            label="Summary"
            choices={[
              { key: "team", label: "A team's" },
              { key: "org", label: "Leadership (all teams)" },
            ]}
            value={scope}
            onChange={(value) => choose("scope", value)}
          />
          <ChoiceSelect
            label="Period"
            choices={[
              { key: "week", label: "Last week" },
              { key: "month", label: "Last month" },
            ]}
            value={period}
            onChange={(value) => choose("period", value)}
          />
          {scope === "team" && team !== null && (
            <ChoiceSelect
              label="Team (for development)"
              choices={teams.map((t) => ({ key: t.key, label: t.name }))}
              value={team.key}
              onChange={(value) => choose("team", value)}
            />
          )}
        </section>
      )}
      {teams !== undefined && teams.length === 0 && scope === "team" && (
        <p className="text-gray-600">This org has no teams yet: there's nothing to summarise.</p>
      )}
      {page.error !== null && (
        <RetryAlert onRetry={page.reload}>The numbers couldn't be loaded: {page.error.message}</RetryAlert>
      )}
      {page.data !== undefined && numbers !== undefined && asOf !== undefined && (
        <>
          <section aria-labelledby="period-heading" className="flex flex-col gap-1">
            <h2 id="period-heading" className="text-lg font-semibold">
              {period === "week" ? "Last week" : "Last month"}:{" "}
              {formatPeriod(period, page.data.period_start, page.data.period_end)}
            </h2>
            <p className="text-sm text-gray-600">Numbers as of {formatDateTime(asOf)}</p>
          </section>
          {summary === null && (
            <button
              type="button"
              onClick={ask}
              disabled={save.saving}
              className="self-start rounded-md bg-blue-700 px-4 py-2 text-white disabled:opacity-50"
            >
              Write the summary
            </button>
          )}
          <SaveError error={save.error} onRetry={save.retry} />
          {/* Always there, so screen readers announce what's put in it. */}
          <p role="status" className={summary?.status === "writing" ? "text-gray-700" : "sr-only"}>
            {summary?.status === "writing" ? "Writing the summary. The numbers below are counted now." : ""}
          </p>
          {polled.error !== null && (
            <RetryAlert onRetry={polled.reload}>The summary couldn't be checked: {polled.error.message}</RetryAlert>
          )}
          {summary?.status === "failed" && (
            <RetryAlert onRetry={() => retry(summary.id)} disabled={save.saving}>
              The summary couldn't be written: {summary.failure_reason}
            </RetryAlert>
          )}
          {ready?.narrative != null && ready.narrative.overview.length > 0 && (
            <section aria-label="Overview" className="flex flex-col gap-1">
              {ready.narrative.overview.map((sentence, index) => (
                <Said key={index} sentence={sentence} />
              ))}
            </section>
          )}
          {"issues" in numbers ? (
            <TeamView
              numbers={numbers}
              narrative={ready?.narrative ?? null}
              period={period}
              teamKey={team?.key ?? ""}
            />
          ) : (
            <OrgView numbers={numbers} narrative={ready?.narrative ?? null} />
          )}
        </>
      )}
    </main>
  );
}

/** A sentence, then each insight it cites, linked to where it was said. */
function Said({ sentence }: { sentence: Sentence }) {
  return (
    <p>
      {sentence.text}
      {sentence.cites.map((cite, index) => (
        <Link
          key={cite.insight_id}
          to={`/visits/${cite.visit_id}#insight-${cite.insight_id}`}
          aria-label={`Finding ${index + 1}, on its visit`}
          className="ml-1 text-sm text-blue-700 underline"
        >
          [{index + 1}]
        </Link>
      ))}
    </p>
  );
}

/** The period's distinct insights, shops, reps and visits; the org's adds triage's. */
function TotalsLine({ totals, triage = false }: { totals: Totals; triage?: boolean }) {
  const counts = [
    countOf(totals.insights, "insight", "insights"),
    countOf(totals.shops, "shop", "shops"),
    countOf(totals.reps, "rep", "reps"),
    countOf(totals.visits, "visit", "visits"),
    ...(triage ? [`${totals.triage} waiting in triage`] : []),
  ];
  return <p className="text-sm text-gray-700">{counts.join(" · ")}</p>;
}

function wordsFor(narrative: Narrative | null, id: string): Sentence[] {
  return narrative?.issues.find((section) => section.id === id)?.sentences ?? [];
}

function TeamView({
  numbers,
  narrative,
  period,
  teamKey,
}: {
  numbers: TeamNumbers;
  narrative: Narrative | null;
  period: PeriodKind;
  teamKey: string;
}) {
  // The drill-down of the very period shown, and the way back to this team's summary.
  const drill = new URLSearchParams({ team: teamKey, team_id: numbers.team_id, period, start: istDay(numbers.period.start) });
  return (
    <>
      <TotalsLine totals={numbers.totals} />
      {numbers.issues.length === 0 && <p className="text-gray-600">Nothing raised in this period.</p>}
      <ol aria-label="Issues, ranked" className="flex flex-col gap-3">
        {numbers.issues.map((issue: IssueNumbers) => (
          <Ranked
            key={issue.issue_id}
            title={issue.title_en}
            measures={issue.measures}
            status={STATUS_LABELS[issue.status]}
            words={wordsFor(narrative, issue.issue_id)}
            drillTo={`/summaries/drill?${drill.toString()}&issue=${issue.issue_id}`}
          />
        ))}
      </ol>
    </>
  );
}

function OrgView({ numbers, narrative }: { numbers: OrgNumbers; narrative: Narrative | null }) {
  const { totals, issues_at_end: atEnd } = numbers;
  return (
    <>
      <TotalsLine totals={totals} triage />
      <p className="text-sm text-gray-700">
        The teams' issues at the period's end: {atEnd.open} open, {atEnd.in_progress} in progress,{" "}
        {atEnd.overdue} overdue. Resolved in the period: {numbers.resolved_in_period}.
      </p>
      {numbers.causes.length === 0 && <p className="text-gray-600">Nothing raised in this period.</p>}
      <ol aria-label="Causes, ranked" className="flex flex-col gap-3">
        {numbers.causes.map((cause: CauseNumbers) => (
          <Ranked
            key={cause.issue_key}
            title={cause.title_en}
            measures={cause.measures}
            status={cause.teams.map((work) => `${work.team_name}: ${STATUS_LABELS[work.status]}`).join(" · ")}
            words={wordsFor(narrative, cause.issue_key)}
          />
        ))}
      </ol>
      {numbers.by_category_area.length > 0 && (
        <section aria-labelledby="areas-heading" className="flex flex-col gap-1">
          <h2 id="areas-heading" className="text-lg font-semibold">
            By category and area
          </h2>
          <ul className="text-sm text-gray-700">
            {numbers.by_category_area.map((row) => (
              <li key={`${row.category}|${row.area}`}>
                {row.category}, {row.area}: {countOf(row.causes, "cause", "causes")},{" "}
                {countOf(row.shops, "shop", "shops")}
              </li>
            ))}
          </ul>
        </section>
      )}
      {numbers.competitors.length > 0 && (
        <section aria-labelledby="competitors-heading" className="flex flex-col gap-1">
          <h2 id="competitors-heading" className="text-lg font-semibold">
            Competitors named
          </h2>
          <ul className="text-sm text-gray-700">
            {numbers.competitors.map((row) => (
              <li key={row.competitor}>
                {row.competitor}: {countOf(row.insights, "insight", "insights")} at{" "}
                {countOf(row.shops, "shop", "shops")}
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}

/** An issue or cause: its numbers, why it ranks where it does, and its sentences. */
function Ranked({
  title,
  measures: m,
  status,
  words,
  drillTo,
}: {
  title: string;
  measures: Measures;
  status: string;
  words: Sentence[];
  /** A team's issue drills down to its reps, shops and quotes (batch 15c). */
  drillTo?: string;
}) {
  return (
    <li className="flex flex-col gap-1 rounded-lg border border-gray-200 p-3">
      <span className="font-medium">{title}</span>
      <span className="flex flex-wrap items-center gap-2 text-sm text-gray-700">
        <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-900">{TREND_LABELS[m.label]}</span>
        {m.severity} · {status}
        {m.overdue && <span className="rounded bg-red-100 px-1.5 py-0.5 text-red-900">Overdue</span>}
      </span>
      <span className="text-sm text-gray-700">
        {countOf(m.shops, "shop", "shops")} · {countOf(m.insights, "insight", "insights")} ·{" "}
        {countOf(m.reps, "rep", "reps")} in the period; usually {countOf(m.baseline, "shop", "shops")}
      </span>
      <span className="text-sm text-gray-600">
        Why it's here: {m.severity} severity, {countOf(m.open_shops, "open shop", "open shops")},{" "}
        {countOf(m.age_weeks, "week", "weeks")} old (score {m.score})
      </span>
      {words.map((sentence, index) => (
        <Said key={index} sentence={sentence} />
      ))}
      {drillTo !== undefined && (
        <Link to={drillTo} className="text-sm text-blue-700 underline">
          Its reps, shops and quotes
        </Link>
      )}
    </li>
  );
}
