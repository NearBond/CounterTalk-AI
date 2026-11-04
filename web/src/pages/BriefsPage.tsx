import { Link, useParams } from "react-router";
import { BriefCard, useShopBrief } from "../components/BriefCard";
import { BriefLink } from "../components/BriefsSection";
import { EMPTY, QUIET_LINK } from "../components/home";
import { RetryAlert } from "../components/RetryAlert";
import { listBriefs } from "../lib/api";
import { usePolling } from "../lib/hooks";

/** /briefs (batch F5), for reps, managers and admins: every shop with its latest brief's state,
 * each opening the shop's brief. */
export function BriefsPage() {
  const briefs = usePolling((signal) => listBriefs(signal), { key: "briefs", shouldPoll: () => false });
  const rows = briefs.data?.items ?? [];
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-6 sm:max-w-3xl sm:py-8">
      <h1 className="text-2xl font-semibold text-ink">Pre-visit briefs</h1>
      <p className="text-ink-2">
        Before a visit, a shop's brief says where it stands: what it raises and orders, what's still open, what got
        fixed, and what's worth raising this time.
      </p>
      {briefs.error !== null && (
        <RetryAlert onRetry={briefs.reload}>The briefs couldn't be loaded: {briefs.error.message}</RetryAlert>
      )}
      {briefs.data === undefined && briefs.error === null && <p className="text-ink-2">Loading…</p>}
      {briefs.data !== undefined && rows.length === 0 && <p className={EMPTY}>No shops yet.</p>}
      {rows.length > 0 && (
        <ul aria-label="Shops" className="flex flex-col gap-2">
          {rows.map((row) => (
            <li key={row.shop_id}>
              <BriefLink row={row} />
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}

/** /briefs/:shopId: one shop's brief, and Generate when allowed. */
export function ShopBriefPage() {
  const { shopId = "" } = useParams();
  // Its own state per shop: another shop's page never shows this one's Generate or error.
  return <ShopBriefView key={shopId} shopId={shopId} />;
}

function ShopBriefView({ shopId }: { shopId: string }) {
  const brief = useShopBrief(shopId);
  const shop = brief.data;
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-6 sm:max-w-3xl sm:py-8">
      <Link to="/briefs" className={`${QUIET_LINK} self-start text-pen`}>
        ← All briefs
      </Link>
      <h1 className="text-2xl font-semibold wrap-break-word text-ink">
        {shop === undefined ? "A shop's brief" : `${shop.name}, ${shop.area}`}
      </h1>
      <BriefCard brief={brief} />
    </main>
  );
}
