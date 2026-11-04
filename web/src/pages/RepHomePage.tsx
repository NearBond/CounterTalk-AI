import { useState } from "react";
import { Link } from "react-router";
import { RetryAlert } from "../components/RetryAlert";
import { StatusBadge } from "../components/StatusBadge";
import { getMyCounts, getMyFixed, getMyHome, getMyPending, listVisits } from "../lib/api";
import { EMPTY, Figure, H2, QUIET_LINK, RECORD, SECONDARY, SHOWN_FIRST, STAT, ViewAll, stat } from "../components/home";
import { formatDateTime, formatDay, formatDuration } from "../lib/format";
import { usePolling, useShops } from "../lib/hooks";
import { STATUS_LABELS } from "../lib/issues";
import { useMe } from "../lib/session";
import { anyProcessing } from "../lib/status";
import type { FixedItem, PendingItem, Shop, ShopFixed, Visit } from "../types/api";

/** `/` for a rep (batch F2; redone for first-time viewers, the user, 4 Nov): who they are, one
 * big Record a visit, their numbers each in plain words, the visits waiting for them to check,
 * what's been fixed for their shops with the line to say, then the rest of their visits. Only
 * the rep's own data: no rankings, no comparisons, no location. */
export function RepHomePage() {
  const me = useMe();
  const visits = usePolling((signal) => listVisits(signal), {
    key: "visits",
    shouldPoll: (page) => anyProcessing(page.items),
  });
  const home = usePolling((signal) => getMyHome(signal), { key: "home", shouldPoll: () => false });
  const counts = usePolling((signal) => getMyCounts(signal), { key: "counts", shouldPoll: () => false });
  const fixed = usePolling((signal) => getMyFixed(signal), { key: "fixed", shouldPoll: () => false });
  const pending = usePolling((signal) => getMyPending(signal), { key: "pending", shouldPoll: () => false });
  // Each list's View all (N), opened inline.
  const [allWaiting, setAllWaiting] = useState(false);
  const [allFixed, setAllFixed] = useState(false);
  const [allPending, setAllPending] = useState(false);
  const shops = useShops();
  const shopById = new Map((shops.data?.items ?? []).map((shop) => [shop.id, shop]));
  const all = visits.data?.items ?? [];
  const waiting = all.filter((visit) => visit.status === "review");
  const rest = all.filter((visit) => visit.status !== "review");
  const fixedShops = fixed.data?.items ?? [];
  const pendingRows = pending.data?.items ?? [];

  const toReview = stat(home.data?.to_review, {
    one: "visit waiting for you to check before teams see it",
    many: "visits waiting for you to check before teams see them",
    none: "No visits waiting for you to check",
    loading: "Visits waiting for you to check",
  });
  const month = stat(counts.data?.raised, {
    one: "problem raised this month",
    many: "problems raised this month",
    none: "Nothing raised yet this month",
    loading: "Problems raised this month",
    tail: `· ${counts.data?.fixed ?? 0} fixed`,
  });
  const passOn = stat(home.data?.to_pass_on, {
    one: "shop has news to pass on",
    many: "shops have news to pass on",
    none: "No news to pass on yet",
    loading: "Shops with news to pass on",
  });

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-8 px-4 py-6 sm:max-w-3xl sm:py-8">
      <div className="flex flex-col gap-1">
        <h1 className="text-3xl leading-tight font-semibold wrap-break-word text-ink">
          Hi, {me.name}{" "}
          <span className="text-ink-3">· {me.org_name}</span>
        </h1>
        <p className="text-ink-2">Your visits and what to tell your shops.</p>
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-6">
        <Link to="/record" className={RECORD}>
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
              <rect x="9" y="3" width="6" height="11" rx="3" />
              <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
            </svg>
          </span>
          Record a visit
          <span aria-hidden="true" className="ml-auto">
            →
          </span>
        </Link>
        <p className="text-sm text-ink-2">
          Talk with the shopkeeper as you always do. You check what the app heard, then it goes to the right team.
        </p>
      </div>

      <ul aria-label="Your numbers" className="grid gap-3 sm:grid-cols-3">
        <li>
          {/* A jump to a section of this page, not a route. */}
          <a href="#ready-to-review" className={STAT}>
            <Figure {...toReview} />
          </a>
        </li>
        <li>
          <Link to="/updates" className={STAT}>
            <Figure {...month} />
          </Link>
        </li>
        <li>
          <Link to="/updates" className={STAT}>
            <Figure {...passOn} />
          </Link>
        </li>
      </ul>

      {visits.error !== null && <RetryAlert onRetry={visits.reload}>{visits.error.message}</RetryAlert>}
      {shops.error !== null && <RetryAlert onRetry={shops.reload}>The shop names couldn't be loaded.</RetryAlert>}
      {visits.data === undefined && visits.error === null && (
        <p role="status" className="text-ink-2">
          Loading…
        </p>
      )}

      {/* On a wide screen: the visits on the left; the fixes to pass on, and what's still pending,
          beside them. */}
      <div className="grid gap-8 lg:grid-cols-2 lg:grid-rows-[auto_1fr] lg:items-start">
        <section aria-labelledby="ready-to-review" className="flex flex-col gap-3 lg:col-start-1 lg:row-start-1">
          <div className="flex flex-col gap-1">
            <h2 id="ready-to-review" className={H2}>
              Ready to review
            </h2>
            <p className="text-sm text-ink-2">
              Check what the app picked up at each shop, then confirm. Teams see it only after that.
            </p>
          </div>
          {visits.data !== undefined && waiting.length === 0 && (
            <p className={EMPTY}>Nothing to check. New recordings show up here once they're processed.</p>
          )}
          {waiting.length > 0 && (
            <ul id="waiting-list" className="flex flex-col gap-2">
              {(allWaiting ? waiting : waiting.slice(0, SHOWN_FIRST)).map((visit) => (
                <li key={visit.id} className="flex items-center gap-4 rounded-lg border border-line bg-card p-4">
                  <VisitFacts visit={visit} shop={shopById.get(visit.shop_id)} />
                  <Link to={`/visits/${visit.id}`} className={SECONDARY}>
                    Review
                  </Link>
                </li>
              ))}
            </ul>
          )}
          <ViewAll count={waiting.length} open={allWaiting} controls="waiting-list" onToggle={setAllWaiting} />
        </section>

        <div className="flex flex-col gap-8 lg:col-start-2 lg:row-span-2 lg:row-start-1">
          {fixed.data !== undefined && (
            <section aria-labelledby="fixed-heading" className="flex flex-col gap-3">
              <div className="flex flex-col gap-1">
                <h2 id="fixed-heading" className={H2}>
                  Fixed since the last visit
                </h2>
                {fixedShops.length > 0 && (
                  <p className="text-sm text-ink-2">Teams fixed what these shops raised. Pass it on at your next visit.</p>
                )}
              </div>
              {fixedShops.length === 0 ? (
                <p className={EMPTY}>
                  Nothing yet. When a team fixes a problem a shop raised, what to tell the shopkeeper shows up here.
                </p>
              ) : (
                <>
                  <ul id="fixed-list" className="flex flex-col gap-3">
                    {(allFixed ? fixedShops : fixedShops.slice(0, SHOWN_FIRST)).map((shop) => (
                      <FixedShop key={shop.shop_id} shop={shop} />
                    ))}
                  </ul>
                  <ViewAll count={fixedShops.length} open={allFixed} controls="fixed-list" onToggle={setAllFixed} />
                  <Link to="/updates" className={`${QUIET_LINK} self-start font-semibold text-pen`}>
                    See all updates →
                  </Link>
                </>
              )}
            </section>
          )}

          {pending.data !== undefined && (
            <section aria-labelledby="pending-heading" className="flex flex-col gap-3">
              <div className="flex flex-col gap-1">
                <h2 id="pending-heading" className={H2}>
                  Pending resolutions
                </h2>
                {pendingRows.length > 0 && (
                  <p className="text-sm text-ink-2">Problems you raised that a team is still working on.</p>
                )}
              </div>
              {pendingRows.length === 0 ? (
                <p className={EMPTY}>Nothing pending: every problem you raised has been fixed.</p>
              ) : (
                <>
                  <ul id="pending-list" className="flex flex-col gap-2">
                    {(allPending ? pendingRows : pendingRows.slice(0, SHOWN_FIRST)).map((row) => (
                      <PendingRow key={`${row.team}|${row.raised_at}|${row.problem}`} row={row} />
                    ))}
                  </ul>
                  <ViewAll count={pendingRows.length} open={allPending} controls="pending-list" onToggle={setAllPending} />
                </>
              )}
            </section>
          )}
        </div>

        <section aria-labelledby="recent-heading" className="flex flex-col gap-3 lg:col-start-1 lg:row-start-2">
          <h2 id="recent-heading" className={H2}>
            Recent visits
          </h2>
          {visits.data !== undefined && all.length === 0 && (
            <p className={EMPTY}>No visits yet. Tap Record a visit to start.</p>
          )}
          {visits.data !== undefined && all.length > 0 && rest.length === 0 && (
            <p className={EMPTY}>No other visits yet.</p>
          )}
          {rest.length > 0 && (
            <ul className="flex flex-col divide-y divide-line overflow-hidden rounded-lg border border-line bg-card">
              {rest.map((visit) => (
                <li key={visit.id}>
                  <Link
                    to={`/visits/${visit.id}`}
                    className="flex min-h-11 items-center gap-3 px-4 py-3 transition-colors hover:bg-paper focus-visible:outline-2 focus-visible:-outline-offset-2 motion-reduce:transition-none"
                  >
                    <VisitFacts visit={visit} shop={shopById.get(visit.shop_id)} />
                    <span className="shrink-0">
                      <StatusBadge status={visit.status} />
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <p className="mt-auto text-sm">
        <Link to="/health" className={`${QUIET_LINK} text-ink-3 hover:text-ink`}>
          Server status
        </Link>
      </p>
    </main>
  );
}

/** One of the rep's problems a team is still working on: where, which team, what, and since when. */
function PendingRow({ row }: { row: PendingItem }) {
  const status =
    row.status === "in_progress" ? "bg-blue-50 text-blue-800" : "bg-amber-100 text-amber-900";
  return (
    <li className="flex flex-col gap-1.5 rounded-lg border border-line bg-card p-4">
      <span className="font-semibold wrap-break-word text-ink">
        {row.shop}
        <span className="font-normal text-ink-2">, {row.area}</span>
      </span>
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-ink-3">
        <span className="rounded-full bg-paper-2 px-2.5 py-0.5 text-xs font-bold text-ink-2">{row.team}</span>
        <span className={`rounded-full px-2.5 py-0.5 text-xs font-bold ${status}`}>{STATUS_LABELS[row.status]}</span>
        <span>raised {formatDay(row.raised_at)}</span>
      </span>
      <span className="text-sm wrap-break-word text-ink-2">Problem: {row.problem}</span>
    </li>
  );
}

/** Where and when a visit was: its shop and area, then its day and length. */
function VisitFacts({ visit, shop }: { visit: Visit; shop: Shop | undefined }) {
  return (
    <span className="flex min-w-0 flex-1 flex-col gap-0.5">
      <span className="font-semibold wrap-break-word text-ink">
        {shop?.name ?? "A shop"}
        {shop !== undefined && <span className="font-normal text-ink-2">, {shop.area}</span>}
      </span>
      <span className="text-sm text-ink-3">
        {formatDateTime(visit.created_at)} · {formatDuration(visit.duration_ms)}
      </span>
    </span>
  );
}

/** One shop's fixes since its last visit, each with the line the shopkeeper hears, the most
 * visible thing on it. */
function FixedShop({ shop }: { shop: ShopFixed }) {
  return (
    <li className="flex flex-col gap-3 rounded-lg border border-line bg-card p-4">
      <h3 className="text-lg leading-snug font-semibold wrap-break-word text-ink">
        {shop.name}
        <span className="font-sans text-base font-normal text-ink-2">, {shop.area}</span>
      </h3>
      <ul className="flex flex-col gap-4">
        {shop.items.map((item) => (
          // No ID comes with it: a team's two issues, resolved together, differ in what they say.
          <Fix key={`${item.team}|${item.category}|${item.resolved_at}|${item.problem}|${item.note}`} item={item} />
        ))}
      </ul>
    </li>
  );
}

function Fix({ item }: { item: FixedItem }) {
  return (
    <li className="flex flex-col gap-1.5">
      <span className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-ink-3">
        <span className="rounded-full bg-leaf-soft px-2.5 py-0.5 text-xs font-bold text-leaf">{item.team}</span>
        <span>fixed {formatDay(item.resolved_at)}</span>
      </span>
      <span className="text-sm wrap-break-word text-ink-2">Problem: {item.problem}</span>
      <p className="border-l-4 border-leaf pl-3 font-display text-lg leading-snug font-semibold wrap-break-word text-ink">
        <span className="font-sans text-sm font-bold tracking-wide text-leaf uppercase">Tell them:</span> {item.note}
      </p>
    </li>
  );
}
