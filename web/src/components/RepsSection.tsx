import { getRepCounts } from "../lib/api";
import { usePolling } from "../lib/hooks";
import type { RepCounts } from "../types/api";
import { CARD, EMPTY, FIGURE_ROW, Figure, H2, stat } from "./home";
import { RetryAlert } from "./RetryAlert";

/** The manager's and admin's home's "Reps": each field sales rep's card, by name, with their
 * visits confirmed and in review, and the issues from their visits still pending. Counts, never a
 * ranking. */
export function RepsSection() {
  const reps = usePolling((signal) => getRepCounts(signal), { key: "reps", shouldPoll: () => false });
  return (
    <section aria-labelledby="reps-heading" className="flex flex-col gap-3">
      <h2 id="reps-heading" className={H2}>
        Reps
      </h2>
      {reps.error !== null && (
        <RetryAlert onRetry={reps.reload}>The reps couldn't be loaded: {reps.error.message}</RetryAlert>
      )}
      {reps.data === undefined && reps.error === null && <p className="text-ink-2">Loading…</p>}
      {reps.data?.length === 0 && <p className={EMPTY}>No field sales reps yet.</p>}
      {reps.data !== undefined && reps.data.length > 0 && (
        <ul aria-label="Reps" className="flex flex-col gap-3">
          {reps.data.map((rep) => (
            <RepCard key={rep.rep_id} rep={rep} />
          ))}
        </ul>
      )}
    </section>
  );
}

function RepCard({ rep }: { rep: RepCounts }) {
  const counts = [
    stat(rep.confirmed, {
      one: "visit confirmed",
      many: "visits confirmed",
      none: "No visits confirmed",
      loading: "Visits confirmed",
    }),
    stat(rep.in_review, {
      one: "visit in review",
      many: "visits in review",
      none: "No visits in review",
      loading: "Visits in review",
    }),
    stat(rep.pending, { one: "issue pending", many: "issues pending", none: "No issues pending", loading: "Issues pending" }),
  ];
  return (
    <li className={`${CARD} flex flex-col gap-3`}>
      <h3 className="text-lg font-semibold wrap-break-word text-ink">{rep.rep_name}</h3>
      <ul aria-label={`${rep.rep_name}'s numbers`} className="grid gap-3 sm:grid-cols-3">
        {counts.map((count) => (
          <li key={count.words} className={FIGURE_ROW}>
            <Figure {...count} arrow={false} />
          </li>
        ))}
      </ul>
    </li>
  );
}
