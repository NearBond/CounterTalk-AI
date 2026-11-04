import { FixedList } from "../components/FixedList";
import { MyCountsLine } from "../components/MyCountsLine";
import { RetryAlert } from "../components/RetryAlert";
import { getMyFixed } from "../lib/api";
import { usePolling } from "../lib/hooks";

/** `/updates`, a rep's own (batch 17e; the user, 3 Nov): their counts this month, then each shop
 * with something fixed since its last visit here, newest fix first, each fix with the line to
 * say. The trust moment for the shopkeeper, kept apart from recording. Batch F7 (the user, 5 Nov:
 * "3 are fixed" over a list of 2): it lists every fix the count counts, those from before the
 * shop's last visit under a plain "Fixed". */
export function UpdatesPage() {
  const loaded = usePolling((signal) => getMyFixed(signal), { key: "fixed", shouldPoll: () => false });
  const shops = loaded.data?.items;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
      <h1 className="text-2xl font-semibold">Updates</h1>
      <MyCountsLine />
      {loaded.error !== null && (
        <RetryAlert onRetry={loaded.reload}>The updates couldn't be loaded: {loaded.error.message}</RetryAlert>
      )}
      {shops === undefined && loaded.error === null && (
        <p role="status" className="text-gray-600">
          Loading…
        </p>
      )}
      {shops !== undefined && shops.length === 0 && <p className="text-gray-600">Nothing new to pass on.</p>}
      {shops?.map((shop) => {
        // A server from before F7 doesn't say: its fixes are all since the last visit.
        const since = shop.items.filter((item) => item.since_last_visit !== false);
        const before = shop.items.filter((item) => item.since_last_visit === false);
        return (
          <section
            key={shop.shop_id}
            aria-labelledby={`fixed-${shop.shop_id}`}
            className="flex flex-col gap-2 rounded-md border border-green-300 bg-green-50 p-3"
          >
            <h2 id={`fixed-${shop.shop_id}`} className="text-lg font-semibold">
              {shop.name}, {shop.area}
            </h2>
            {since.length > 0 && (
              <>
                <p className="text-sm text-gray-700">Fixed since the last visit here</p>
                <FixedList items={since} />
              </>
            )}
            {before.length > 0 && (
              <>
                <p className="text-sm text-gray-700">Fixed</p>
                <FixedList items={before} />
              </>
            )}
          </section>
        );
      })}
    </main>
  );
}
