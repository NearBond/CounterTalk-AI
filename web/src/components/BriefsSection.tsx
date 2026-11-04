import { Link } from "react-router";
import { listBriefs } from "../lib/api";
import { briefStateText } from "../lib/briefs";
import { formatDay } from "../lib/format";
import { usePolling } from "../lib/hooks";
import type { BriefRow } from "../types/api";
import { EMPTY, FOCUS, H2, QUIET_LINK, SHOWN_FIRST } from "./home";
import { RetryAlert } from "./RetryAlert";

/** The homes' "Pre-visit briefs" (batch F5): the rep's, the manager's and the admin's. The latest
 * SHOWN_FIRST briefs, out-of-date ones marked, then View all on /briefs. */
export function BriefsSection() {
  const briefs = usePolling((signal) => listBriefs(signal), { key: "briefs", shouldPoll: () => false });
  // The list puts the shops with a brief first, newest first.
  const latest = (briefs.data?.items ?? []).filter((row) => row.generated_at !== null).slice(0, SHOWN_FIRST);
  return (
    <section aria-labelledby="briefs-heading" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4">
        <h2 id="briefs-heading" className={H2}>
          Pre-visit briefs
        </h2>
        <Link to="/briefs" className={`${QUIET_LINK} font-semibold text-pen`}>
          View all
        </Link>
      </div>
      {briefs.error !== null && (
        <RetryAlert onRetry={briefs.reload}>The briefs couldn't be loaded: {briefs.error.message}</RetryAlert>
      )}
      {briefs.data === undefined && briefs.error === null && <p className="text-ink-2">Loading…</p>}
      {briefs.data !== undefined && latest.length === 0 && (
        <p className={EMPTY}>
          No briefs yet. Once a shop has 2 confirmed visits, its brief can be generated: before the next visit,
          it says where the shop stands.
        </p>
      )}
      {latest.length > 0 && (
        <ul aria-label="Latest briefs" className="flex flex-col gap-2">
          {latest.map((row) => (
            <li key={row.shop_id}>
              <BriefLink row={row} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** A shop's row, linking to its brief: the briefs page's and the homes'. */
export function BriefLink({ row }: { row: BriefRow }) {
  const stale = row.state === "out_of_date";
  return (
    <Link
      to={`/briefs/${encodeURIComponent(row.shop_id)}`}
      className={`flex min-h-11 flex-col gap-1 rounded-lg border border-line bg-card px-4 py-3 transition-colors hover:border-ink motion-reduce:transition-none ${FOCUS}`}
    >
      <span className="font-semibold wrap-break-word text-ink">
        {row.name}, {row.area}
      </span>
      <span className="text-sm text-ink-2">
        {row.generated_at !== null && `Generated ${formatDay(row.generated_at)} · `}
        <span className={stale ? "font-semibold text-vermilion-ink" : undefined}>{briefStateText(row)}</span>
      </span>
    </Link>
  );
}
