import { useEffect, useState } from "react";
import { getFixed } from "../lib/api";
import { formatDay } from "../lib/format";
import type { FixedItem } from "../types/api";

/** On the shop pick, before Start (batch 17b; the user, 3 Nov): what got fixed for this shop since
 * its last visit, each with the team's note as the line to say. The trust moment for the
 * shopkeeper. Nothing shows while it loads, when nothing was fixed, or if it can't be read: it's
 * a help, never in the way of recording. */
export function FixedSinceLastVisit({ shopId }: { shopId: string }) {
  // Kept with the shop it's for: another pick shows nothing until its own list comes.
  const [found, setFound] = useState<{ shopId: string; items: FixedItem[] } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getFixed(shopId, controller.signal)
      .then((page) => setFound({ shopId, items: page.items }))
      .catch(() => {}); // a help only: the record screen works without it
    return () => controller.abort();
  }, [shopId]);

  const items = found?.shopId === shopId ? found.items : [];
  if (items.length === 0) return null;
  return (
    <section aria-labelledby="fixed-heading" className="flex flex-col gap-2 rounded-md border border-green-300 bg-green-50 p-3">
      <h2 id="fixed-heading" className="text-lg font-semibold">
        Fixed since the last visit here
      </h2>
      <ul className="flex flex-col gap-3">
        {items.map((item) => (
          // No ID comes with it: a team's two issues, resolved together, differ in what they say.
          <li
            key={`${item.team}|${item.category}|${item.resolved_at}|${item.problem}|${item.note}`}
            className="flex flex-col gap-1"
          >
            <span className="text-sm text-gray-700">
              {item.team} · {formatDay(item.resolved_at)}
            </span>
            <span>{item.problem}</span>
            <span className="font-medium">Tell them: {item.note}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
