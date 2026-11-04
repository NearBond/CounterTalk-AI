import { Link } from "react-router";
import { RetryAlert } from "../components/RetryAlert";
import { VisitList } from "../components/VisitList";
import { listVisits } from "../lib/api";
import { shopLabel } from "../lib/format";
import { usePolling, useShops } from "../lib/hooks";
import { anyProcessing } from "../lib/status";

/** `/` for a manager or admin: the org's visits, newest first, polled only while one of them is
 * processing (§9.12). A rep's home is RepHomePage (batch F2). */
export function VisitsPage() {
  const visits = usePolling((signal) => listVisits(signal), {
    key: "visits",
    shouldPoll: (page) => anyProcessing(page.items),
  });
  const shops = useShops();
  const shopName = new Map((shops.data?.items ?? []).map((shop) => [shop.id, shopLabel(shop)]));

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-6 px-4 py-8">
      <h1 className="text-2xl font-semibold">Visits</h1>

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

      <VisitList visits={visits.data?.items ?? []} shopName={shopName} />

      <div className="flex gap-4 text-sm">
        <Link to="/health" className="text-gray-500 underline">
          Server status
        </Link>
      </div>
    </main>
  );
}
