/** Issues as the app shows them (batch 15). */
import type { IssueStatus } from "../types/api";

export const STATUS_LABELS: Record<IssueStatus, string> = {
  open: "Open",
  in_progress: "In progress",
  resolved: "Resolved",
  merged: "Merged",
};

/** The statuses a team member picks: merging is batch 15c's. */
export const SETTABLE: Exclude<IssueStatus, "merged">[] = ["open", "in_progress", "resolved"];

/** A server time as a date input's value (YYYY-MM-DD), in the device's own time zone. */
export function dateInputOf(iso: string): string {
  const at = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
}

/** A date input's value as the end of that day in the device's time zone, with its offset, as
 * the server wants a due date. */
export function dueAtOf(day: string): string {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year ?? 0, (month ?? 1) - 1, date ?? 1, 23, 59, 0).toISOString();
}
