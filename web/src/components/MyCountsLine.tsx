import { useEffect, useState } from "react";
import { getMyCounts } from "../lib/api";
import { countOf } from "../lib/format";
import type { MyCounts } from "../types/api";

/** The rep's own line at the top of the shop pick (batch 17b): "This month you raised 12
 * problems. 7 are fixed." Only their own numbers, never a ranking; hidden when they raised none. */
export function MyCountsLine() {
  const [counts, setCounts] = useState<MyCounts | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getMyCounts(controller.signal)
      .then(setCounts)
      .catch(() => {}); // a help only
    return () => controller.abort();
  }, []);

  if (counts === null || counts.raised === 0) return null;
  return (
    <p className="text-gray-700">
      This month you raised {countOf(counts.raised, "problem", "problems")}. {counts.fixed}{" "}
      {counts.fixed === 1 ? "is" : "are"} fixed.
    </p>
  );
}
