import { useId, useState } from "react";
import { ApiError, generateBrief, getShopBrief } from "../lib/api";
import { countOf, formatDay } from "../lib/format";
import { type Save, usePolling, useSave } from "../lib/hooks";
import { STATUS_LABELS } from "../lib/issues";
import type { Brief, ShopBrief } from "../types/api";
import { ISSUE_PILLS, PILL, SECONDARY } from "./home";
import { RetryAlert } from "./RetryAlert";

/* The pre-visit brief (batch F5): where a shop stands before the next visit. The facts come from
 * the database; Muse Spark writes only the overview and what's worth raising, and only when
 * someone taps Generate (or Regenerate, once a confirmed visit came since). Reading one is free. */

export interface ShopBriefLoad {
  data: ShopBrief | undefined;
  error: ApiError | null;
  reload: () => void;
  generate: () => void;
  save: Save;
}

/** A shop's brief, loaded once, and Generate. No shop picked (""), nothing is asked. */
export function useShopBrief(shopId: string): ShopBriefLoad {
  const loaded = usePolling((signal) => getShopBrief(shopId, signal), {
    key: `brief:${shopId}`,
    shouldPoll: () => false,
    skip: shopId === "",
  });
  // The answer of a Generate, kept over what was loaded for the same shop.
  const [made, setMade] = useState<ShopBrief | null>(null);
  const save = useSave();
  const { reload } = loaded;
  const generate = () =>
    save.run(async (signal) => {
      try {
        return await generateBrief(shopId, signal);
      } catch (failure) {
        // Someone else's brief came first, or a visit was unconfirmed: what's there now.
        if (failure instanceof ApiError && failure.status === 409) {
          setMade(null);
          reload();
        }
        throw failure;
      }
    }, setMade);
  const data = made !== null && made.shop_id === shopId ? made : loaded.data;
  return { data, error: loaded.error, reload, generate, save };
}

/** Generate's failure: Try again only where trying again could help. */
function retryable(error: ApiError): boolean {
  return error.status !== 409 && error.code !== "llm_not_configured";
}

interface Props {
  brief: ShopBriefLoad;
  /** The record screen's: the overview and what to raise, the rest behind More. */
  compact?: boolean;
}

/** A shop's "Pre-visit brief" card: its brief, or why there's none, and Generate when allowed. */
/** One shop's card, with its own load and Generate: give it `key={shopId}`, so a shop picked
 * next never shows the last one's Generate, its error or its Try again. */
export function ShopBriefCard({ shopId, compact = false }: { shopId: string; compact?: boolean }) {
  return <BriefCard brief={useShopBrief(shopId)} compact={compact} />;
}

export function BriefCard({ brief: load, compact = false }: Props) {
  const heading = useId();
  const found = load.data;
  const { save } = load;
  return (
    <section aria-labelledby={heading} className="flex flex-col gap-3 rounded-lg border border-line bg-card p-4">
      <h2 id={heading} className="text-lg font-semibold text-ink">
        Pre-visit brief
      </h2>
      {load.error !== null && (
        <RetryAlert onRetry={load.reload}>The brief couldn't be loaded: {load.error.message}</RetryAlert>
      )}
      {found === undefined && load.error === null && <p className="text-ink-2">Loading…</p>}
      {found !== undefined && (
        <>
          {found.brief !== null && <BriefBody brief={found.brief} since={found.since} compact={compact} />}
          {found.brief === null && found.state === "not_enough_visits" && (
            <p className="text-ink-2">
              A brief needs at least 2 confirmed visits to this shop. So far:{" "}
              {countOf(found.visits, "confirmed visit", "confirmed visits")}.
            </p>
          )}
          {found.brief === null && found.state !== "not_enough_visits" && (
            <p className="text-ink-2">
              No brief yet. Generate writes one from this shop's visits: what it raises and orders, what's
              still open and fixed, and what's worth raising this time.
            </p>
          )}
          {found.can_generate && (
            <button
              type="button"
              onClick={load.generate}
              disabled={save.saving}
              className={`${SECONDARY} self-start disabled:opacity-60`}
            >
              {save.saving ? "Writing the brief…" : found.brief === null ? "Generate" : "Regenerate"}
            </button>
          )}
          {save.error !== null && (
            <RetryAlert onRetry={retryable(save.error) ? save.retry : undefined} disabled={save.saving}>
              The brief couldn't be written: {save.error.message}
            </RetryAlert>
          )}
        </>
      )}
    </section>
  );
}

const H3 = "font-semibold text-ink";
const QUIET = "text-ink-2";

/** The brief itself: when it was written, then its five parts in order. */
function BriefBody({ brief, since, compact }: { brief: Brief; since: number; compact: boolean }) {
  const facts = (
    <>
      <LastVisit brief={brief} />
      <StillOpen brief={brief} />
      <FixedSince brief={brief} />
    </>
  );
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-ink-2">
        Generated on {formatDay(brief.generated_at)}, after visit {brief.after_visit}.
        {since > 0 && (
          <span className="font-semibold text-vermilion-ink"> {countOf(since, "visit", "visits")} since.</span>
        )}
      </p>
      <div className="flex flex-col gap-1">
        <h3 className={H3}>Shop overview</h3>
        {brief.overview.length > 0 ? (
          <p className="text-ink">{brief.overview.join(" ")}</p>
        ) : (
          <p className={QUIET}>No overview this time: the facts are below.</p>
        )}
      </div>
      {compact ? (
        <details className="flex flex-col gap-4">
          <summary className="min-h-11 cursor-pointer py-2 font-semibold text-pen">
            Last visit, still open and fixed since
          </summary>
          <div className="flex flex-col gap-4 pt-2">{facts}</div>
        </details>
      ) : (
        facts
      )}
      <div className="flex flex-col gap-1">
        <h3 className={H3}>Worth raising this time</h3>
        {brief.worth_raising.length > 0 ? (
          <p className="text-ink">{brief.worth_raising.join(" ")}</p>
        ) : (
          <p className={QUIET}>Nothing to suggest this time.</p>
        )}
      </div>
    </div>
  );
}

function LastVisit({ brief }: { brief: Brief }) {
  const { raised, ordered, visited_at } = brief.last_visit;
  return (
    <div className="flex flex-col gap-2">
      <h3 className={H3}>Last visit · {formatDay(visited_at)}</h3>
      <div className="flex flex-col gap-1">
        <h4 className="text-sm font-semibold text-ink-2">What was raised</h4>
        {raised.length === 0 ? (
          <p className={QUIET}>Nothing raised.</p>
        ) : (
          <ul aria-label="What was raised" className="flex list-disc flex-col gap-1 pl-5">
            {raised.map((item, index) => (
              <li key={index} className="wrap-break-word">
                <span className="font-medium">{item.category}:</span> {item.problem}
                {item.quote !== null && <q className="block text-ink-2 italic">{item.quote}</q>}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <h4 className="text-sm font-semibold text-ink-2">What was ordered</h4>
        {ordered.length === 0 ? (
          <p className={QUIET}>Nothing ordered.</p>
        ) : (
          <ul aria-label="What was ordered" className="flex list-disc flex-col gap-1 pl-5">
            {ordered.map((item, index) => (
              <li key={index} className="wrap-break-word">
                {item.name}
                {item.quantity !== null && ` · ${item.quantity}${item.unit === null ? "" : ` ${item.unit}`}`}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function StillOpen({ brief }: { brief: Brief }) {
  return (
    <div className="flex flex-col gap-2">
      <h3 className={H3}>Still open</h3>
      {brief.still_open.length === 0 ? (
        <p className={QUIET}>Nothing still open.</p>
      ) : (
        <ul aria-label="Still open" className="flex flex-col gap-2">
          {brief.still_open.map((item, index) => (
            <li key={index} className="flex flex-col gap-1 wrap-break-word">
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{item.team}</span>
                <span className={`${PILL} ${ISSUE_PILLS[item.status]}`}>{STATUS_LABELS[item.status]}</span>
                <span className="text-sm text-ink-2">raised {formatDay(item.raised_at)}</span>
              </span>
              <span>{item.problem}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FixedSince({ brief }: { brief: Brief }) {
  return (
    <div className="flex flex-col gap-2">
      <h3 className={H3}>Fixed since</h3>
      {brief.fixed_since.length === 0 ? (
        <p className={QUIET}>Nothing fixed since the last visit.</p>
      ) : (
        <ul aria-label="Fixed since" className="flex flex-col gap-2">
          {brief.fixed_since.map((item, index) => (
            <li key={index} className="flex flex-col gap-1 wrap-break-word">
              <span>
                <span className="font-medium">{item.team}</span>
                <span className="text-sm text-ink-2"> · fixed {formatDay(item.resolved_at)}</span>
              </span>
              <span>{item.problem}</span>
              <span className="font-semibold">Tell them: {item.tell_them}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
