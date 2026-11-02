/** The only place times, durations and names are formatted (CODING_GUIDELINES §4.9). Times
 * are IST. */
import type { PeriodKind, Shop } from "../types/api";

/** "Sunrise Kirana, Old Market": a shop as the rep knows it. */
export function shopLabel(shop: Shop): string {
  return `${shop.name}, ${shop.area}`;
}

/** 0:05, 3:16, 1:02:05; a dash for a duration not known yet. */
export function formatDuration(ms: number | null): string {
  if (ms === null) return "–";
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
}

const DATE_TIME = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "numeric",
  minute: "2-digit",
  hour12: true,
});

/** 1 Nov 2026, 11:30 am, in India's time whatever the browser's time zone. */
export function formatDateTime(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "–";
  const parts = Object.fromEntries(
    DATE_TIME.formatToParts(when).map((part) => [part.type, part.value]),
  );
  return `${parts.day} ${parts.month} ${parts.year}, ${parts.hour}:${parts.minute} ${String(parts.dayPeriod).toLowerCase()}`;
}

const IST = "Asia/Kolkata";
const DAY_MONTH = new Intl.DateTimeFormat("en-IN", { timeZone: IST, day: "numeric", month: "short" });
const YEAR = new Intl.DateTimeFormat("en-IN", { timeZone: IST, year: "numeric" });
const MONTH_YEAR = new Intl.DateTimeFormat("en-IN", { timeZone: IST, month: "long", year: "numeric" });

/** A summary's period, [start, end) in IST: "26 Oct – 1 Nov 2026" for a week (with both years
 * when it crosses one), "October 2026" for a month. */
export function formatPeriod(kind: PeriodKind, start: string, end: string): string {
  const first = new Date(start);
  if (kind === "month") return MONTH_YEAR.format(first);
  const last = new Date(new Date(end).getTime() - 1);
  const [from, to] = [YEAR.format(first), YEAR.format(last)];
  return `${DAY_MONTH.format(first)}${from === to ? "" : ` ${from}`} – ${DAY_MONTH.format(last)} ${to}`;
}

const ISO_DAY = new Intl.DateTimeFormat("en-CA", { timeZone: IST, year: "numeric", month: "2-digit", day: "2-digit" });

/** 2026-10-26: a moment's day in IST, as the API takes a period's start. */
export function istDay(iso: string): string {
  return ISO_DAY.format(new Date(iso));
}

/** "1 shop", "3 shops", "0.5 shops": a count and its noun, the one way the app words one (R1). */
export function countOf(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}
