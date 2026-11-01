import { useState } from "react";
import { Link } from "react-router";
import {
  ApiError,
  acceptRoute,
  alsoSend,
  audioUrl,
  changeIssue,
  getIssue,
  moveRoute,
} from "../lib/api";
import { formatDateTime } from "../lib/format";
import { usePlayer, usePolling, useSave } from "../lib/hooks";
import { dateInputOf, dueAtOf, SETTABLE, STATUS_LABELS } from "../lib/issues";
import {
  type Choice,
  type IssueChange,
  type IssueDetail,
  type Member,
  RESOLUTION_NOTE_MAX_CHARS,
  type Team,
} from "../types/api";
import { ChoiceSelect } from "./ChoiceSelect";
import { PagePlayer } from "./PagePlayer";
import { QuoteLine } from "./QuoteLine";
import { RetryAlert } from "./RetryAlert";
import { SaveError } from "./SaveError";

interface Props {
  issueId: string;
  teams: Team[];
  /** The list's cards are out of date: ask for them again. */
  onChanged: () => void;
}

// A change made on an old read: shown, and the issue read again, so the page catches up.
const STALE = new Set(["issue_changed", "route_changed"]);

/** One issue, expanded: its status, owner and due date to change, and each of its insights. */
export function IssuePanel({ issueId, teams, onChanged }: Props) {
  const loaded = usePolling((signal) => getIssue(issueId, signal), { key: issueId, shouldPoll: () => false });
  const { playFrom, playAt: _, src, ...player } = usePlayer();

  if (loaded.error !== null) {
    return <RetryAlert onRetry={loaded.reload}>The issue couldn't be loaded: {loaded.error.message}</RetryAlert>;
  }
  if (loaded.data === undefined) {
    return (
      <p role="status" className="text-gray-600">
        Loading…
      </p>
    );
  }
  const detail = loaded.data;
  const changed = () => {
    loaded.reload();
    onChanged();
  };

  return (
    <div className="flex flex-col gap-4 border-t border-gray-200 pt-3">
      {/* Keyed on its version: read again, its form starts from what's saved now. */}
      <IssueControls key={detail.issue.version} detail={detail} onSaved={changed} onStale={changed} />
      {src !== null && <PagePlayer {...player} src={src} />}
      <ul aria-label="Its insights" className="flex flex-col gap-3">
        {detail.members.map((member) => (
          <MemberItem
            key={`${member.insight.id}:${member.route.version}`}
            member={member}
            teams={teams}
            onPlay={(ms) => playFrom(audioUrl(member.visit_id), ms)}
            onRouted={changed}
          />
        ))}
      </ul>
    </div>
  );
}

function IssueControls({
  detail,
  onSaved,
  onStale,
}: {
  detail: IssueDetail;
  onSaved: () => void;
  onStale: () => void;
}) {
  const { issue } = detail;
  const [status, setStatus] = useState<string>(issue.status === "merged" ? "open" : issue.status);
  const [note, setNote] = useState("");
  const [due, setDue] = useState(dateInputOf(issue.due_at));
  const save = useSave();
  const closed = issue.status === "resolved" || issue.status === "merged";
  const noOwner = issue.owner_id === null;
  const send = (change: Omit<IssueChange, "version">) =>
    save.run(
      (signal) => changeIssue(issue.id, { version: issue.version, ...change }, signal),
      () => {
        setNote("");
        onSaved();
      },
    );
  const failed = save.error;
  const stale = failed instanceof ApiError && STALE.has(failed.code);
  const statusChoices: Choice[] = SETTABLE.map((key) => ({ key, label: STATUS_LABELS[key] }));

  return (
    <section aria-label="Status" className="flex flex-col gap-2">
      <p className="text-sm">
        Owner: {issue.owner_name ?? "nobody yet"} · Due {formatDateTime(issue.due_at)}
        {issue.overdue && <span className="font-medium text-red-700"> · Overdue</span>}
      </p>
      {issue.resolution_note !== null && <p className="text-sm">Answer: {issue.resolution_note}</p>}
      {stale && (
        <RetryAlert onRetry={onStale}>Someone changed it meanwhile: your change wasn't saved.</RetryAlert>
      )}
      {!stale && <SaveError error={failed} onRetry={save.retry} />}
      {!closed && (
        <>
          <button
            type="button"
            disabled={save.saving}
            onClick={() => send({ assign_to_me: true })}
            className="self-start rounded-md border border-gray-300 px-3 py-1.5 text-sm disabled:opacity-50"
          >
            Assign to me
          </button>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              send(
                status === "resolved"
                  ? { status: "resolved", note }
                  : { status: status as "open" | "in_progress" },
              );
            }}
            className="flex flex-col gap-2"
          >
            <ChoiceSelect
              label="Its status"
              choices={statusChoices}
              value={status}
              disabled={save.saving}
              onChange={setStatus}
            />
            {status === "resolved" && (
              <label className="flex flex-col gap-1 text-sm">
                How it was resolved (the rep tells the shop)
                <textarea
                  value={note}
                  maxLength={RESOLUTION_NOTE_MAX_CHARS}
                  disabled={save.saving}
                  onChange={(event) => setNote(event.target.value)}
                  className="rounded-md border border-gray-300 p-2"
                  rows={3}
                />
              </label>
            )}
            <button
              type="submit"
              disabled={save.saving || status === issue.status || (status === "resolved" && note.trim() === "")}
              className="self-start rounded-md bg-blue-700 px-3 py-1.5 text-white disabled:opacity-50"
            >
              {save.saving ? "Saving…" : "Save status"}
            </button>
          </form>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              send({ due_at: dueAtOf(due) });
            }}
            className="flex flex-wrap items-end gap-2"
          >
            <label className="flex flex-col gap-1 text-sm">
              {noOwner ? "Due date (assign it first: its owner changes it)" : "Due date (its owner's to change)"}
              <input
                type="date"
                value={due}
                disabled={save.saving || noOwner}
                onChange={(event) => setDue(event.target.value)}
                className="rounded-md border border-gray-300 p-2"
              />
            </label>
            <button
              type="submit"
              disabled={save.saving || noOwner || due === "" || due === dateInputOf(issue.due_at)}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm disabled:opacity-50"
            >
              Change due date
            </button>
          </form>
        </>
      )}
    </section>
  );
}

function MemberItem({
  member,
  teams,
  onPlay,
  onRouted,
}: {
  member: Member;
  teams: Team[];
  onPlay: (ms: number) => void;
  onRouted: () => void;
}) {
  const { insight, route } = member;
  const others = teams.filter((team) => team.id !== route.team_id);
  // One picker for Move and Also send, preselected to the AI's team when it differs.
  const suggested = others.find((team) => team.key === insight.ai_team);
  const [target, setTarget] = useState(suggested?.id ?? others[0]?.id ?? "");
  const save = useSave();
  const seen = { slot: route.slot, version: route.version };
  const full = insight.routes.length >= 2;
  const act = (work: (signal: AbortSignal) => Promise<unknown>) => save.run(work, onRouted);
  const failed = save.error;
  const stale = failed instanceof ApiError && STALE.has(failed.code);
  const choices: Choice[] = others.map((team) => ({ key: team.id, label: team.name }));
  const quote = insight.quotes[0];

  return (
    <li className="flex flex-col gap-2 rounded-lg border border-gray-200 p-3">
      <p className="text-xs text-gray-600">
        {member.shop_name} ({member.area}) · {member.rep_name} · {formatDateTime(member.visited_at)}
      </p>
      <p>{insight.summary_en}</p>
      {quote !== undefined && <QuoteLine quote={quote} onPlay={onPlay} />}
      <Link to={`/visits/${member.visit_id}`} className="text-sm text-blue-700 underline">
        Open the visit
      </Link>
      {route.routed_by === "ai" && (
        <p className="flex items-center gap-2 text-sm">
          Suggested by AI{insight.ai_team_reason !== null && `: ${insight.ai_team_reason}`}
          <button
            type="button"
            disabled={save.saving}
            onClick={() => act((signal) => acceptRoute(insight.id, seen, signal))}
            className="rounded-md border border-gray-300 px-2 py-1 disabled:opacity-50"
          >
            Accept
          </button>
        </p>
      )}
      {stale && <RetryAlert onRetry={onRouted}>Someone routed it meanwhile: nothing was changed.</RetryAlert>}
      {!stale && <SaveError error={failed} onRetry={save.retry} />}
      {choices.length > 0 && (
        <div className="flex flex-wrap items-end gap-2">
          <ChoiceSelect label="Team" choices={choices} value={target} disabled={save.saving} onChange={setTarget} />
          <button
            type="button"
            disabled={save.saving}
            onClick={() => act((signal) => moveRoute(insight.id, { ...seen, team_id: target }, signal))}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm disabled:opacity-50"
          >
            Move
          </button>
          <button
            type="button"
            disabled={save.saving || full}
            title={full ? "It already has two teams" : undefined}
            onClick={() => act((signal) => alsoSend(insight.id, { ...seen, team_id: target }, signal))}
            className="rounded-md border border-gray-300 px-3 py-1.5 text-sm disabled:opacity-50"
          >
            Also send
          </button>
        </div>
      )}
    </li>
  );
}
