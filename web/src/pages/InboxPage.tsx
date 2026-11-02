import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { ChoiceSelect } from "../components/ChoiceSelect";
import { IssuePanel } from "../components/IssuePanel";
import { RetryAlert } from "../components/RetryAlert";
import { getInboxOptions, type IssueFilters, listIssues } from "../lib/api";
import { countOf, formatDateTime } from "../lib/format";
import { usePolling } from "../lib/hooks";
import { STATUS_LABELS } from "../lib/issues";
import { INBOX_POLL_MS } from "../lib/limits";
import type { Choice, Issue } from "../types/api";

const ANY: Choice = { key: "", label: "Any" };
const FILTERS = ["status", "category", "area", "distributor"] as const;

/** `/inbox`: a team's issues (batch 15), asked for again every INBOX_POLL_MS while the tab is
 * visible. The team is the dev dropdown's until batch 16 signs a member in. */
export function InboxPage() {
  const [params, setParams] = useSearchParams();
  const options = usePolling((signal) => getInboxOptions(signal), { key: "options", shouldPoll: () => false });
  // The address's values, only where the pickers offer them, so what's shown is what's asked.
  const data = options.data;
  const wanted = params.get("team");
  const team =
    data === undefined ? null : (data.teams.find((t) => t.key === wanted) ?? data.teams[0])?.key ?? null;
  const offered: Record<(typeof FILTERS)[number], string[]> = {
    status: ["resolved"],
    category: data?.categories.map((c) => c.key) ?? [],
    area: data?.areas ?? [],
    distributor: data?.distributors.map((d) => d.id) ?? [],
  };
  const filters: IssueFilters = {};
  for (const name of FILTERS) {
    const value = params.get(name);
    if (value && offered[name].includes(value)) Object.assign(filters, { [name]: value });
  }
  const issues = usePolling((signal) => listIssues(team ?? "", filters, signal), {
    key: JSON.stringify([team, filters]),
    shouldPoll: () => true,
    intervalMs: INBOX_POLL_MS,
    skip: team === null,
  });
  const [open, setOpen] = useState<string | null>(null);
  // An issue that leaves the list says where it went: resolved here, or (the open card gone after
  // a refresh) resolved by someone else or moved away. Hidden at once, before the list catches up.
  const [resolved, setResolved] = useState<Issue | null>(null);
  const shown = issues.data?.filter((issue) => issue.id !== resolved?.id);
  const left = open !== null && shown !== undefined && !shown.some((issue) => issue.id === open);
  const notice =
    resolved !== null
      ? `Resolved: ${resolved.title_en}. It's under Status: Resolved now.`
      : left
        ? "That issue has left this list: someone resolved it, or its findings went to another team."
        : "";
  const status = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (resolved !== null) status.current?.focus(); // where the card was, read out
  }, [resolved]);
  const onResolved = (issue: Issue) => {
    setResolved(issue);
    setOpen(null);
    issues.reload();
  };
  const toggle = (issueId: string) => {
    setResolved(null);
    setOpen(open === issueId ? null : issueId);
  };
  const choose = (name: string, value: string) => {
    setResolved(null);
    setOpen(null);
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next, { replace: true });
  };

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
      <div className="flex gap-4">
        <Link to="/" className="text-blue-700 underline">
          Visits
        </Link>
        <Link to="/inbox/triage" className="text-blue-700 underline">
          Triage
        </Link>
        <Link to="/summaries" className="text-blue-700 underline">
          Summaries
        </Link>
      </div>
      <h1 className="text-2xl font-semibold">Inbox</h1>
      {options.error !== null && (
        <RetryAlert onRetry={options.reload}>The inbox couldn't be loaded: {options.error.message}</RetryAlert>
      )}
      {data !== undefined && team !== null && (
        <section aria-label="Filters" className="grid grid-cols-2 gap-2">
          <ChoiceSelect
            label="Team (for development)"
            choices={data.teams.map((t) => ({ key: t.key, label: t.name }))}
            value={team}
            onChange={(value) => choose("team", value)}
          />
          <ChoiceSelect
            label="Status"
            choices={[
              { key: "", label: "Open and in progress" },
              { key: "resolved", label: STATUS_LABELS.resolved },
            ]}
            value={filters.status ?? ""}
            onChange={(value) => choose("status", value)}
          />
          <ChoiceSelect
            label="Category"
            choices={[ANY, ...data.categories]}
            value={filters.category ?? ""}
            onChange={(value) => choose("category", value)}
          />
          <ChoiceSelect
            label="Area"
            choices={[ANY, ...data.areas.map((area) => ({ key: area, label: area }))]}
            value={filters.area ?? ""}
            onChange={(value) => choose("area", value)}
          />
          <ChoiceSelect
            label="Distributor"
            choices={[ANY, ...data.distributors.map((d) => ({ key: d.id, label: d.name }))]}
            value={filters.distributor ?? ""}
            onChange={(value) => choose("distributor", value)}
          />
        </section>
      )}
      {/* Always there, so screen readers announce what's put in it. */}
      <p
        ref={status}
        role="status"
        tabIndex={-1}
        className={notice === "" ? "sr-only" : "rounded-md bg-green-50 p-3 text-green-900"}
      >
        {notice}
      </p>
      {issues.error !== null && (
        <RetryAlert onRetry={issues.reload}>The issues couldn't be loaded: {issues.error.message}</RetryAlert>
      )}
      {shown !== undefined && shown.length === 0 && <p className="text-gray-600">No issues here.</p>}
      {shown !== undefined && data !== undefined && (
        <ul aria-label="Issues" className="flex flex-col gap-3">
          {shown.map((issue) => (
            <li key={issue.id} className="flex flex-col gap-2 rounded-lg border border-gray-200 p-3">
              <IssueSummary
                issue={issue}
                expanded={open === issue.id}
                onToggle={() => toggle(issue.id)}
              />
              {open === issue.id && (
                <IssuePanel issueId={issue.id} teams={data.teams} onChanged={issues.reload} onResolved={onResolved} />
              )}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}

function IssueSummary({ issue, expanded, onToggle }: { issue: Issue; expanded: boolean; onToggle: () => void }) {
  const counts = [
    countOf(issue.shops, "shop", "shops"),
    countOf(issue.insights, "insight", "insights"),
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

