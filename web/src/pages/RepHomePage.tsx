import type { ReactNode } from "react";
import { Link } from "react-router";
import { RetryAlert } from "../components/RetryAlert";
import { VisitList } from "../components/VisitList";
import { getMyCounts, getMyFixed, getMyHome, listVisits } from "../lib/api";
import { countOf, shopLabel } from "../lib/format";
import { usePolling, useShops } from "../lib/hooks";
import { useMe } from "../lib/session";
import { anyProcessing } from "../lib/status";

// The fixes the home shows before "See all updates →".
const FIXED_ON_HOME = 3;
const CARD = "flex min-h-20 flex-col justify-between gap-1 rounded-lg border p-3 focus-visible:outline-2 focus-visible:outline-offset-2";

/** `/` for a rep (batch F2; the user, 4 Nov): a greeting, cards for recording and their own
 * numbers, the visits waiting for review, the newest fixes to pass on, then the rest of their
 * visits. Only the rep's own data: no rankings, no comparisons, no location. */
export function RepHomePage() {
  const me = useMe();
  const visits = usePolling((signal) => listVisits(signal), {
    key: "visits",
    shouldPoll: (page) => anyProcessing(page.items),
  });
  const home = usePolling((signal) => getMyHome(signal), { key: "home", shouldPoll: () => false });
  const counts = usePolling((signal) => getMyCounts(signal), { key: "counts", shouldPoll: () => false });
  const fixed = usePolling((signal) => getMyFixed(signal), { key: "fixed", shouldPoll: () => false });
  const shops = useShops();
  const shopName = new Map((shops.data?.items ?? []).map((shop) => [shop.id, shopLabel(shop)]));
  const all = visits.data?.items ?? [];
  const waiting = all.filter((visit) => visit.status === "review");
  const rest = all.filter((visit) => visit.status !== "review");
  const newest = (fixed.data?.items ?? [])
    .flatMap((shop) => shop.items.map((item) => ({ shop, item })))
    .sort((a, b) => b.item.resolved_at.localeCompare(a.item.resolved_at))
    .slice(0, FIXED_ON_HOME);
  const month =
    counts.data === undefined
      ? "–"
      : counts.data.raised === 0
        ? "Nothing raised yet this month"
        : `${counts.data.raised} raised · ${counts.data.fixed} fixed`;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-6 px-4 py-8 sm:max-w-2xl">
      <h1 className="text-2xl font-semibold">
        Hi, {me.name} · {me.org_name}
      </h1>

      <ul aria-label="Shortcuts" className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <li>
          <Link to="/record" className={`${CARD} border-red-700 bg-red-700 font-medium text-white`}>
            Record a visit
          </Link>
        </li>
        <Card to="/updates" title="This month" value={month} />
        <Card to="#ready-to-review" title="To review" value={home.data?.to_review ?? "–"} />
        <Card
          to="/updates"
          title="To pass on"
          value={home.data === undefined ? "–" : countOf(home.data.to_pass_on, "shop", "shops")}
        />
      </ul>

      {visits.error !== null && <RetryAlert onRetry={visits.reload}>{visits.error.message}</RetryAlert>}
      {shops.error !== null && <RetryAlert onRetry={shops.reload}>The shop names couldn't be loaded.</RetryAlert>}
      {visits.data === undefined && visits.error === null && (
        <p role="status" className="text-gray-600">
          Loading…
        </p>
      )}

      <section aria-labelledby="ready-to-review" className="flex flex-col gap-2">
        <h2 id="ready-to-review" className="text-lg font-semibold">
          Ready to review
        </h2>
        {visits.data !== undefined && waiting.length === 0 && <p className="text-gray-600">Nothing waiting for review.</p>}
        <VisitList
          visits={waiting}
          shopName={shopName}
          extra={() => <span className="text-sm font-medium text-blue-700 underline">Review</span>}
        />
      </section>

      {newest.length > 0 && (
        <section aria-labelledby="fixed-heading" className="flex flex-col gap-2">
          <h2 id="fixed-heading" className="text-lg font-semibold">
            Fixed since the last visit
          </h2>
          <ul className="flex flex-col gap-3 rounded-md border border-green-300 bg-green-50 p-3">
            {newest.map(({ shop, item }) => (
              <li key={`${shop.shop_id}|${item.team}|${item.resolved_at}|${item.problem}`} className="flex flex-col gap-1">
                <span className="text-sm text-gray-700">
                  {shop.name} · {item.team}
                </span>
                <span className="font-medium">Tell them: {item.note}</span>
              </li>
            ))}
          </ul>
          <Link to="/updates" className="text-blue-700 underline">
            See all updates →
          </Link>
        </section>
      )}

      <section aria-labelledby="recent-heading" className="flex flex-col gap-2">
        <h2 id="recent-heading" className="text-lg font-semibold">
          Recent visits
        </h2>
        {visits.data !== undefined && all.length === 0 && <p className="text-gray-600">No visits yet.</p>}
        <VisitList visits={rest} shopName={shopName} />
      </section>

      <div className="flex gap-4 text-sm">
        <Link to="/health" className="text-gray-500 underline">
          Server status
        </Link>
      </div>
    </main>
  );
}

function Card({ to, title, value }: { to: string; title: string; value: ReactNode }) {
  const inside = (
    <>
      <span className="text-sm text-gray-600">{title}</span>{" "}
      <span className="font-semibold">{value}</span>
    </>
  );
  return (
    <li>
      {to.startsWith("#") ? (
        // A jump to a section of this page, not a route.
        <a href={to} className={`${CARD} border-gray-200 hover:bg-gray-50`}>
          {inside}
        </a>
      ) : (
        <Link to={to} className={`${CARD} border-gray-200 hover:bg-gray-50`}>
          {inside}
        </Link>
      )}
    </li>
  );
}
