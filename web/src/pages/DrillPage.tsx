import { Link, useSearchParams } from "react-router";
import { PagePlayer } from "../components/PagePlayer";
import { QuoteLine } from "../components/QuoteLine";
import { RetryAlert } from "../components/RetryAlert";
import { audioUrl, getDrill } from "../lib/api";
import { countOf, formatDateTime, formatPeriod } from "../lib/format";
import { usePlayer, usePolling } from "../lib/hooks";
import { isRetryable } from "../lib/retry";
import { issueLink, STATUS_LABELS } from "../lib/issues";
import type { PeriodKind } from "../types/api";

/** `/summaries/drill?team=<id>&issue=<id>&period=week|month` (batch 15c): a team summary's
 * issue, its reps, their shops and visits, and each quote with ▶ on the page's one player. From
 * SQL, with no AI words; counted now. Reps are listed by name: never ranked or compared. */
export function DrillPage() {
  const [params] = useSearchParams();
  const teamKey = params.get("team") ?? "";
  const teamId = params.get("team_id");
  const issueId = params.get("issue");
  const start = params.get("start");
  const period: PeriodKind = params.get("period") === "month" ? "month" : "week";
  const complete = teamId !== null && issueId !== null;
  const found = usePolling((signal) => getDrill(teamId ?? "", issueId ?? "", period, start, signal), {
    key: JSON.stringify([teamId, issueId, period, start]),
    shouldPoll: () => false,
    skip: !complete,
  });
  const { playFrom, playAt: _, src, ...player } = usePlayer();
  const data = found.data;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
      <div className="flex gap-4">
        <Link to={`/summaries?${new URLSearchParams({ team: teamKey, period }).toString()}`} className="text-blue-700 underline">
          Summaries
        </Link>
        <Link to={issueId === null ? "/inbox" : issueLink(issueId, { team: teamKey })} className="text-blue-700 underline">
          Inbox
        </Link>
      </div>
      {!complete && (
        <p className="text-gray-600">Open an issue from a team's summary to see its reps, shops and quotes.</p>
      )}
      {found.error !== null && (
        // A 404 (merged or moved since the summary was written) won't change: no Try again.
        <RetryAlert onRetry={isRetryable(found.error) ? found.reload : undefined}>
          The issue couldn't be loaded: {found.error.message}
        </RetryAlert>
      )}
      {complete && data === undefined && found.error === null && (
        <p role="status" className="text-gray-600">
          Loading…
        </p>
      )}
      {data !== undefined && (
        <>
          <h1 className="text-2xl font-semibold">{data.title_en}</h1>
          <p className="text-sm text-gray-600">
            {period === "week" ? "Last week" : "Last month"}:{" "}
            {formatPeriod(period, data.period_start, data.period_end)} · {STATUS_LABELS[data.status]} · counted{" "}
            {formatDateTime(data.numbers_as_of)}
          </p>
          <p className="text-sm text-gray-700">
            Since it was last opened: {countOf(data.shops, "shop", "shops")} ·{" "}
            {countOf(data.insights, "insight", "insights")} · {countOf(data.reps, "rep", "reps")} ·{" "}
            {countOf(data.raised_again, "shop raising it again", "shops raising it again")}
          </p>
          <p className="text-sm text-gray-700">
            {period === "week" ? "That week" : "That month"}: {countOf(data.in_period.shops, "shop", "shops")} ·{" "}
            {countOf(data.in_period.insights, "insight", "insights")} ·{" "}
            {countOf(data.in_period.reps, "rep", "reps")}
          </p>
          {src !== null && <PagePlayer {...player} src={src} />}
          {data.by_rep.map((rep) => (
            <section key={rep.rep_id} aria-labelledby={`rep-${rep.rep_id}`} className="flex flex-col gap-2">
              <h2 id={`rep-${rep.rep_id}`} className="text-lg font-semibold">
                {rep.rep_name}: {countOf(rep.shops.length, "shop", "shops")},{" "}
                {countOf(rep.insights, "insight", "insights")}
              </h2>
              <ul aria-label={`${rep.rep_name}'s shops`} className="flex flex-col gap-3">
                {rep.shops.map((shop) => (
                  <li key={shop.shop_id} className="flex flex-col gap-2 rounded-lg border border-gray-200 p-3">
                    <span className="font-medium">
                      {shop.name}, {shop.area}
                      {shop.distributor !== null && ` · ${shop.distributor}`}
                    </span>
                    {shop.visits.map((visit) => (
                      <div key={visit.visit_id} className="flex flex-col gap-1">
                        <Link to={`/visits/${visit.visit_id}`} className="text-sm text-blue-700 underline">
                          {formatDateTime(visit.visited_at)}
                          {visit.in_period && (period === "week" ? " (that week)" : " (that month)")}
                        </Link>
                        {visit.insights.map((insight) => (
                          <div key={insight.id} className="flex flex-col gap-1">
                            <p>{insight.summary_en}</p>
                            {insight.quotes[0] !== undefined && (
                              <QuoteLine
                                quote={insight.quotes[0]}
                                onPlay={(ms) => playFrom(audioUrl(visit.visit_id), ms)}
                              />
                            )}
                          </div>
                        ))}
                      </div>
                    ))}
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </>
      )}
    </main>
  );
}
