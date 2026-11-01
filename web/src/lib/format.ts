/** The only place times and durations are formatted (CODING_GUIDELINES §4.9). Times are IST. */

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
