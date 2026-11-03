import { useState } from "react";
import { ChoiceSelect } from "../components/ChoiceSelect";
import { PagePlayer } from "../components/PagePlayer";
import { QuoteLine } from "../components/QuoteLine";
import { RetryAlert } from "../components/RetryAlert";
import { SaveError } from "../components/SaveError";
import { ApiError, assignTeam, audioUrl, getInboxOptions, listTriage } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { usePlayer, usePolling, useSave } from "../lib/hooks";
import { INBOX_POLL_MS } from "../lib/limits";
import type { Team, Finding } from "../types/api";

/** `/inbox/triage`: confirmed insights no team was found for (batch 15). Someone gives each a
 * team, and it joins that team's issue. */
export function TriagePage() {
  const options = usePolling((signal) => getInboxOptions(signal), { key: "options", shouldPoll: () => false });
  const waiting = usePolling((signal) => listTriage(signal), {
    key: "triage",
    shouldPoll: () => true,
    intervalMs: INBOX_POLL_MS,
  });
  const { playFrom, playAt: _, src, ...player } = usePlayer();
  const teams = options.data?.teams;

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
      <h1 className="text-2xl font-semibold">Triage</h1>
      <p className="text-sm text-gray-600">Findings no team was found for. Give each one a team.</p>
      {options.error !== null && (
        <RetryAlert onRetry={options.reload}>The teams couldn't be loaded: {options.error.message}</RetryAlert>
      )}
      {waiting.error !== null && (
        <RetryAlert onRetry={waiting.reload}>Triage couldn't be loaded: {waiting.error.message}</RetryAlert>
      )}
      {src !== null && <PagePlayer {...player} src={src} />}
      {waiting.data !== undefined && waiting.data.length === 0 && <p className="text-gray-600">Nothing waiting.</p>}
      {waiting.data !== undefined && teams !== undefined && (
        <ul aria-label="Waiting for a team" className="flex flex-col gap-3">
          {waiting.data.map((item) => (
            <TriageItem
              key={item.insight.id}
              item={item}
              teams={teams}
              onPlay={(ms) => playFrom(audioUrl(item.visit_id), ms)}
              onAssigned={waiting.reload}
            />
          ))}
        </ul>
      )}
    </main>
  );
}

function TriageItem({
  item,
  teams,
  onPlay,
  onAssigned,
}: {
  item: Finding;
  teams: Team[];
  onPlay: (ms: number) => void;
  onAssigned: () => void;
}) {
  const { insight } = item;
  const suggested = teams.find((team) => team.key === insight.ai_team);
  const [team, setTeam] = useState(suggested?.id ?? teams[0]?.id ?? "");
  const save = useSave();
  const failed = save.error;
  const taken = failed instanceof ApiError && failed.code === "already_routed";
  const quote = insight.quotes[0];

  return (
    <li className="flex flex-col gap-2 rounded-lg border border-gray-200 p-3">
      <p className="text-xs text-gray-600">
        {item.shop_name} ({item.area}) · {item.rep_name} · {formatDateTime(item.visited_at)}
      </p>
      <p>{insight.summary_en}</p>
      {quote !== undefined && <QuoteLine quote={quote} onPlay={onPlay} />}
      {taken && <RetryAlert onRetry={onAssigned}>Someone gave it a team meanwhile.</RetryAlert>}
      {!taken && <SaveError error={failed} onRetry={save.retry} />}
      <div className="flex flex-wrap items-end gap-2">
        <ChoiceSelect
          label="Team"
          choices={teams.map((each) => ({ key: each.id, label: each.name }))}
          value={team}
          disabled={save.saving}
          onChange={setTeam}
        />
        <button
          type="button"
          disabled={save.saving || team === ""}
          onClick={() => save.run((signal) => assignTeam(insight.id, team, signal), onAssigned)}
          className="rounded-md bg-blue-700 px-3 py-1.5 text-white disabled:opacity-50"
        >
          {save.saving ? "Saving…" : "Give it this team"}
        </button>
      </div>
    </li>
  );
}
