import { STATUS, type Tone } from "../lib/status";
import type { VisitStatus } from "../types/api";

const TONE: Record<Tone, string> = {
  busy: "bg-amber-100 text-amber-900",
  good: "bg-leaf-soft text-leaf",
  bad: "bg-vermilion-soft text-vermilion-ink",
};

/** A visit's status as v2's pill: a dot in its tone, then the words. */
export function StatusBadge({ status }: { status: VisitStatus }) {
  const { label, tone } = STATUS[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 self-start rounded-full px-2 py-0.5 text-xs font-bold ${TONE[tone]}`}
    >
      <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-current" />
      {label}
    </span>
  );
}
