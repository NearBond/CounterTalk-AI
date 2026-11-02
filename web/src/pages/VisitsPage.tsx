import { Link } from "react-router";
import { RetryAlert } from "../components/RetryAlert";
import { StatusBadge } from "../components/StatusBadge";
import { listVisits } from "../lib/api";
import { formatDateTime, formatDuration, shopLabel } from "../lib/format";
import { usePolling, useShops } from "../lib/hooks";
import { anyProcessing } from "../lib/status";
import { RECORDS, useMe } from "../lib/session";

/** `/`: the rep's visits, newest first, polled only while one of them is processing (§9.12). */
export function VisitsPage() {
  const visits = usePolling((signal) => listVisits(signal), {
    key: "visits",
    shouldPoll: (page) => anyProcessing(page.items),
  });
  const me = useMe();
  const shops = useShops();
  const shopName = new Map((shops.data?.items ?? []).map((shop) => [shop.id, shopLabel(shop)]));

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-6 px-4 py-8">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">Visits</h1>
        {RECORDS.includes(me.role) && (
          <Link
            to="/record"
            className="rounded-md bg-red-700 px-4 py-2 font-medium text-white focus-visible:outline-2 focus-visible:outline-offset-2"
          >
            Record a visit
          </Link>
        )}
      </div>

      {visits.error !== null && (
        <RetryAlert onRetry={visits.reload}>{visits.error.message}</RetryAlert>
      )}
      {shops.error !== null && (
        <RetryAlert onRetry={shops.reload}>The shop names couldn't be loaded.</RetryAlert>
      )}
      {visits.data === undefined && visits.error === null && (
        <p role="status" className="text-gray-600">
          Loading…
        </p>
      )}
      {visits.data?.items.length === 0 && <p className="text-gray-600">No visits yet.</p>}

      <ul className="flex flex-col divide-y divide-gray-200">
        {visits.data?.items.map((visit) => (
          <li key={visit.id}>
            <Link to={`/visits/${visit.id}`} className="flex flex-col gap-1 py-3 hover:bg-gray-50">
              <span className="font-medium">{shopName.get(visit.shop_id) ?? "A shop"}</span>
              <span className="text-sm text-gray-600">
                {formatDateTime(visit.created_at)} · {formatDuration(visit.duration_ms)}
              </span>
              <StatusBadge status={visit.status} />
            </Link>
          </li>
        ))}
      </ul>

      <div className="flex gap-4 text-sm">
        <Link to="/health" className="text-gray-500 underline">
          Server status
        </Link>
      </div>
    </main>
  );
}
