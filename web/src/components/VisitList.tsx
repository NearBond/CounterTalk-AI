import type { ReactNode } from "react";
import { Link } from "react-router";
import { formatDateTime, formatDuration } from "../lib/format";
import type { Visit } from "../types/api";
import { StatusBadge } from "./StatusBadge";

interface Props {
  visits: Visit[];
  /** Each shop's name by its ID: "A shop" until the names load. */
  shopName: Map<string, string>;
  label?: string;
  /** What each row ends with: the rep's home adds "Review" (batch F2). */
  extra?: (visit: Visit) => ReactNode;
}

/** Visits as rows, each linking to its page: the visits list's, and the rep's home's (R1). */
export function VisitList({ visits, shopName, label, extra }: Props) {
  return (
    <ul aria-label={label} className="flex flex-col divide-y divide-gray-200">
      {visits.map((visit) => (
        <li key={visit.id}>
          <Link to={`/visits/${visit.id}`} className="flex flex-col gap-1 py-3 hover:bg-gray-50">
            <span className="font-medium">{shopName.get(visit.shop_id) ?? "A shop"}</span>
            <span className="text-sm text-gray-600">
              {formatDateTime(visit.created_at)} · {formatDuration(visit.duration_ms)}
            </span>
            <StatusBadge status={visit.status} />
            {extra?.(visit)}
          </Link>
        </li>
      ))}
    </ul>
  );
}
