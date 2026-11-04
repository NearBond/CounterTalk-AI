import type { BriefState } from "../types/api";
import { countOf } from "./format";

/** A brief's state in plain words (batch F5): the briefs list's, and the homes'. */
export function briefStateText(row: { state: BriefState; since: number }): string {
  switch (row.state) {
    case "up_to_date":
      return "Up to date";
    case "out_of_date":
      return `${countOf(row.since, "visit", "visits")} since`;
    case "not_enough_visits":
      return "Not enough visits yet";
    case "none":
      return "No brief yet";
  }
}
