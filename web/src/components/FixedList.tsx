import { formatDay } from "../lib/format";
import type { FixedItem } from "../types/api";

/** A shop's fixes since its last visit here (batch 17b's items, on Updates since 17e): the team
 * and day, the problem, and the team's note as the line to say. */
export function FixedList({ items }: { items: FixedItem[] }) {
  return (
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
  );
}
