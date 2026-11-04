import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import { AskBox } from "../components/AskBox";
import { ChoiceSelect } from "../components/ChoiceSelect";
import { IssuePanel } from "../components/IssuePanel";
import { RetryAlert } from "../components/RetryAlert";
import { getInboxOptions, type IssueFilters, listIssues } from "../lib/api";
import { countOf, formatDateTime } from "../lib/format";
import { usePolling } from "../lib/hooks";
import { STATUS_LABELS } from "../lib/issues";
import { INBOX_POLL_MS } from "../lib/limits";
import { useMe, wholeOrg } from "../lib/session";
import { type Choice, type InboxPeriod, type Issue, type IssueDetail, PERIOD_FIRST_YEAR, PERIOD_LAST_YEAR } from "../types/api";

const ANY: Choice = { key: "", label: "Any" };
const FILTERS = ["status", "category", "area", "distributor", "competitor", "product"] as const;
// The period picker's own days (batch 17c): any From and To, each bounded by the other. It's
// in the address as period=custom, so a link or Back shows what was picked.
const CUSTOM = "custom";
const IST_DAY = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;
const FIRST_DAY = `${PERIOD_FIRST_YEAR}-01-01`;
const LAST_DAY = `${PERIOD_LAST_YEAR}-12-31`;

/** `/inbox`: a team's issues (batch 15), asked for again every INBOX_POLL_MS while the tab is
 * visible. A team member's are their own team's; a manager or admin picks the team (batch 16). */
export function InboxPage() {
  const me = useMe();
  const picks = wholeOrg(me);
  const [params, setParams] = useSearchParams();
  const options = usePolling((signal) => getInboxOptions(signal), { key: "options", shouldPoll: () => false });
  // The address's values, only where the pickers offer them, so what's shown is what's asked.
  const data = options.data;
  const wanted = params.get("team");
  const team = !picks
    ? (me.team?.key ?? null)
    : data === undefined
      ? null
      : ((data.teams.find((t) => t.key === wanted) ?? data.teams[0])?.key ?? null);
  const offered: Record<(typeof FILTERS)[number], string[]> = {
    status: ["open", "in_progress", "resolved"],
    category: data?.categories.map((c) => c.key) ?? [],
    area: data?.areas ?? [],
    distributor: data?.distributors.map((d) => d.id) ?? [],
    competitor: data?.competitors.map((c) => c.id) ?? [],
    product: data?.products.map((p) => p.id) ?? [],
  };
  const filters: IssueFilters = {};
  for (const name of FILTERS) {
    const value = params.get(name);
    if (value && offered[name].includes(value)) Object.assign(filters, { [name]: value });
  }
  // The address's days count only as real days in the server's years, in order: a period that
  // ends before it starts is none.
  const from = realDay(params.get("from"));
  const to = realDay(params.get("to"));
  if (from === undefined || to === undefined || from <= to) {
    if (from !== undefined) filters.from = from;
    if (to !== undefined) filters.to = to;
  }
  const period = params.get("period") === CUSTOM ? CUSTOM : periodKey(data?.periods ?? [], filters);
  const issues = usePolling((signal) => listIssues(picks ? team : null, filters, signal), {
    key: JSON.stringify([team, filters]),
    shouldPoll: () => true,
    intervalMs: INBOX_POLL_MS,
    // The address's filters count only once their choices are known (`offered`).
    skip: team === null || data === undefined,
  });
  // The issue a link opened (batch F7: issueLink): expanded, scrolled to once and highlighted,
  // until anything else is picked.
  const target = params.get("issue");
  const [open, setOpen] = useState<string | null>(target);
  const [marked, setMarked] = useState<string | null>(target);
  const reached = useRef(false);
  // An issue that leaves the list says where it went: resolved here, merged here (batch 15c), or
  // (the open card gone after a refresh) resolved by someone else or moved away. The card it's
  // about is hidden at once, before the list catches up.
  const [notice, setNotice] = useState<{ text: string; hide: string } | null>(null);
  const shown = issues.data?.filter((issue) => issue.id !== notice?.hide);
  const left = open !== null && shown !== undefined && !shown.some((issue) => issue.id === open);
  const said =
    notice?.text ??
    (left ? "That issue has left this list: its status changed, or its findings went to another team." : "");
  const status = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (notice !== null) status.current?.focus(); // where the card was, read out
  }, [notice]);
  useEffect(() => {
    if (marked === null || reached.current || !shown?.some((issue) => issue.id === marked)) return;
    reached.current = true;
    const card = document.getElementById(`issue-${marked}`);
    card?.scrollIntoView?.({ block: "start" });
    card?.querySelector("button")?.focus({ preventScroll: true });
  }, [marked, shown]);
  // Anything else picked: the link's issue is done with, and leaves the address.
  const unmark = (next: URLSearchParams) => {
    setMarked(null);
    next.delete("issue");
  };
  const onResolved = (issue: Issue) => {
    setNotice({ text: `Resolved: ${issue.title_en}. It's under Status: Resolved now.`, hide: issue.id });
    setOpen(null);
    issues.reload();
  };
  const onMerged = (kept: IssueDetail, mergedId: string) => {
    setNotice({ text: `Merged: their findings are together in ${kept.issue.title_en}.`, hide: mergedId });
    setOpen(kept.issue.id);
    issues.reload();
  };
  const toggle = (issueId: string) => {
    setNotice(null);
    setOpen(open === issueId ? null : issueId);
    if (marked !== null) {
      const next = new URLSearchParams(params);
      unmark(next);
      setParams(next, { replace: true });
    }
  };
  const choose = (changes: Record<string, string>) => {
    setNotice(null);
    setOpen(null);
    const next = new URLSearchParams(params);
    unmark(next);
    for (const [name, value] of Object.entries(changes)) {
      if (value) next.set(name, value);
      else next.delete(name);
    }
    setParams(next, { replace: true });
  };
  const pickPeriod = (key: string) => {
    if (key === CUSTOM) {
      choose({ period: CUSTOM }); // its days are picked next, starting from the current ones
      return;
    }
    const found = data?.periods.find((p) => p.key === key);
    choose({ period: "", from: found?.first_day ?? "", to: found?.last_day ?? "" });
  };
  // Moving one end past the other moves both: the period never ends before it starts.
  const pickFrom = (day: string) =>
    choose(day && filters.to !== undefined && day > filters.to ? { from: day, to: day } : { from: day });
  const pickTo = (day: string) =>
    choose(day && filters.from !== undefined && day < filters.from ? { from: day, to: day } : { to: day });

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
      <h1 className="text-2xl font-semibold">Inbox</h1>
      {/* Batch 17d: across the whole org for a manager or admin; a team member's own team's. */}
      <AskBox />
      {options.error !== null && (
        <RetryAlert onRetry={options.reload}>The inbox couldn't be loaded: {options.error.message}</RetryAlert>
      )}
      {data !== undefined && team !== null && (
        <section aria-label="Filters" className="grid grid-cols-2 gap-2">
          {picks && (
            <ChoiceSelect
              label="Team"
              choices={data.teams.map((t) => ({ key: t.key, label: t.name }))}
              value={team}
              onChange={(value) => choose({ team: value })}
            />
          )}
          <ChoiceSelect
            label="Status"
            choices={[
              { key: "", label: "Open and in progress" },
              { key: "open", label: STATUS_LABELS.open },
              { key: "in_progress", label: STATUS_LABELS.in_progress },
              { key: "resolved", label: STATUS_LABELS.resolved },
            ]}
            value={filters.status ?? ""}
            onChange={(value) => choose({ status: value })}
          />
          <ChoiceSelect
            label="Category"
            choices={[ANY, ...data.categories]}
            value={filters.category ?? ""}
            onChange={(value) => choose({ category: value })}
          />
          <ChoiceSelect
            label="Area"
            choices={[ANY, ...data.areas.map((area) => ({ key: area, label: area }))]}
            value={filters.area ?? ""}
            onChange={(value) => choose({ area: value })}
          />
          <ChoiceSelect
            label="Distributor"
            choices={[ANY, ...data.distributors.map((d) => ({ key: d.id, label: d.name }))]}
            value={filters.distributor ?? ""}
            onChange={(value) => choose({ distributor: value })}
          />
          <ChoiceSelect
            label="Competitor"
            choices={[ANY, ...data.competitors.map((c) => ({ key: c.id, label: c.name }))]}
            value={filters.competitor ?? ""}
            onChange={(value) => choose({ competitor: value })}
          />
          <ChoiceSelect
            label="Product"
            choices={[ANY, ...data.products.map((p) => ({ key: p.id, label: p.name }))]}
            value={filters.product ?? ""}
            onChange={(value) => choose({ product: value })}
          />
          <ChoiceSelect
            label="Period"
            choices={[
              ANY,
              ...data.periods.map((p) => ({ key: p.key, label: p.label })),
              { key: CUSTOM, label: "Custom dates" },
            ]}
            value={period}
            onChange={pickPeriod}
          />
          {period === CUSTOM && (
            <>
              <DayField label="From" value={filters.from} min={FIRST_DAY} max={filters.to ?? LAST_DAY} onChange={pickFrom} />
              <DayField label="To" value={filters.to} min={filters.from ?? FIRST_DAY} max={LAST_DAY} onChange={pickTo} />
            </>
          )}
        </section>
      )}
      {/* Always there, so screen readers announce what's put in it. */}
      <p
        ref={status}
        role="status"
        tabIndex={-1}
        className={said === "" ? "sr-only" : "rounded-md bg-green-50 p-3 text-green-900"}
      >
        {said}
      </p>
      {issues.error !== null && (
        <RetryAlert onRetry={issues.reload}>The issues couldn't be loaded: {issues.error.message}</RetryAlert>
      )}
      {shown !== undefined && shown.length === 0 && <p className="text-gray-600">No issues here.</p>}
      {shown !== undefined && data !== undefined && (
        <ul aria-label="Issues" className="flex flex-col gap-3">
          {shown.map((issue) => (
            <li
              key={issue.id}
              id={`issue-${issue.id}`}
              className={`flex scroll-mt-4 flex-col gap-2 rounded-lg border p-3 ${
                marked === issue.id ? "border-ink ring-2 ring-marigold" : "border-gray-200"
              }`}
            >
              <IssueSummary
                issue={issue}
                expanded={open === issue.id}
                onToggle={() => toggle(issue.id)}
              />
              {open === issue.id && (
                <IssuePanel issueId={issue.id} teams={data.teams} onChanged={issues.reload} onResolved={onResolved} onMerged={onMerged} />
              )}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}

/** The ready-made period the address's days are, Custom dates for any others, or "" for none. */
function periodKey(periods: InboxPeriod[], { from, to }: IssueFilters): string {
  if (from === undefined && to === undefined) return "";
  return periods.find((p) => p.first_day === from && p.last_day === to)?.key ?? CUSTOM;
}

/** The value if it's a real day ("2026-02-30" isn't) in the server's years, YYYY-MM-DD. */
function realDay(value: string | null): string | undefined {
  if (value === null || !IST_DAY.test(value) || value < FIRST_DAY || value > LAST_DAY) return undefined;
  const when = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(when.getTime()) && when.toISOString().slice(0, 10) === value ? value : undefined;
}

interface DayFieldProps {
  label: string;
  value: string | undefined;
  min: string;
  max: string;
  onChange: (day: string) => void;
}

/** One end of a custom period: an IST day, as the date picker gives it (YYYY-MM-DD). */
function DayField({ label, value, min, max, onChange }: DayFieldProps) {
  return (
    <label className="flex min-w-0 flex-col gap-1 text-sm">
      {label}
      <input
        type="date"
        value={value ?? ""}
        min={min}
        max={max}
        onChange={(event) => onChange(event.target.value)}
        className="w-full min-w-0 rounded-md border border-gray-300 p-2"
      />
    </label>
  );
}

function IssueSummary({ issue, expanded, onToggle }: { issue: Issue; expanded: boolean; onToggle: () => void }) {
  const counts = [
    countOf(issue.shops, "shop", "shops"),
    countOf(issue.insights, "finding", "findings"),
    countOf(issue.reps, "rep", "reps"),
  ].join(" · ");
  return (
    <button type="button" aria-expanded={expanded} onClick={onToggle} className="flex flex-col gap-1 text-left">
      <span className="font-medium">{issue.title_en}</span>
      <span className="text-sm text-gray-700">
        {issue.severity} · {STATUS_LABELS[issue.status]} · {counts}
      </span>
      <span className="flex flex-wrap gap-2 text-xs">
        {issue.raised_again && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-900">Raised again</span>}
        {issue.overdue && <span className="rounded bg-red-100 px-1.5 py-0.5 text-red-900">Overdue</span>}
        <span className="text-gray-600">
          {issue.owner_name ?? "No owner"} · due {formatDateTime(issue.due_at)}
        </span>
      </span>
    </button>
  );
}

