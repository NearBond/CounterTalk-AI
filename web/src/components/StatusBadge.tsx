import { STATUS, type Tone } from "../lib/status";
import type { VisitStatus } from "../types/api";

const TONE: Record<Tone, string> = {
  busy: "bg-amber-100 text-amber-900",
  good: "bg-green-100 text-green-900",
  bad: "bg-red-100 text-red-900",
};

export function StatusBadge({ status }: { status: VisitStatus }) {
  const { label, tone } = STATUS[status];
  return (
    <span className={`self-start rounded-full px-2 py-0.5 text-xs font-medium ${TONE[tone]}`}>{label}</span>
  );
}
