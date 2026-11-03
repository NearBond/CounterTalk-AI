import { type FormEvent, useState } from "react";
import { Link } from "react-router";
import { askQuestion, audioUrl } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { usePlayer, useSave } from "../lib/hooks";
import { STATUS_LABELS } from "../lib/issues";
import { ASK_QUESTION_MAX_CHARS, type AskAnswer, type Finding } from "../types/api";
import { PagePlayer } from "./PagePlayer";
import { QuoteLine } from "./QuoteLine";
import { RetryAlert } from "./RetryAlert";

/** The inbox's Ask (batch 17d): a question across visits, in Hindi, Marathi or English. Muse
 * Spark only turns it into a filter; the server checks it and runs it. The chips say what was
 * understood, read-only, then the findings, each with its quote's ▶. A manager's or admin's are
 * the whole org's; a team member's, their own team's. */
export function AskBox() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<AskAnswer | null>(null);
  const save = useSave();
  const { playFrom, playAt: _, src, ...player } = usePlayer();
  const asked = question.trim();
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (asked === "" || save.saving) return;
    setAnswer(null);
    save.run((signal) => askQuestion(asked, signal), setAnswer);
  };
  const failed = save.error;

  return (
    <section aria-labelledby="ask-heading" className="flex flex-col gap-2 rounded-lg border border-gray-200 p-3">
      <h2 id="ask-heading" className="text-lg font-semibold">
        Ask a question
      </h2>
      <form onSubmit={submit} className="flex flex-col gap-2">
        <label className="flex flex-col gap-1 text-sm">
          In Hindi, Marathi or English
          <input
            type="text"
            value={question}
            maxLength={ASK_QUESTION_MAX_CHARS}
            placeholder="Lay's pricing complaints in Old Market this month"
            onChange={(event) => setQuestion(event.target.value)}
            className="w-full min-w-0 rounded-md border border-gray-300 p-2"
          />
        </label>
        <button
          type="submit"
          disabled={asked === "" || save.saving}
          className="self-start rounded-md bg-blue-700 px-4 py-2 text-white disabled:opacity-50"
        >
          {save.saving ? "Asking…" : "Ask"}
        </button>
      </form>
      {/* No Try again: each question counts against the day's, and Ask sends what's typed now. */}
      {failed !== null && <RetryAlert small>{failed.message}</RetryAlert>}
      {answer !== null && <Answer answer={answer} onPlay={(visitId, ms) => playFrom(audioUrl(visitId), ms)} />}
      {src !== null && <PagePlayer {...player} src={src} />}
    </section>
  );
}

function Answer({ answer, onPlay }: { answer: AskAnswer; onPlay: (visitId: string, ms: number) => void }) {
  if (!answer.understood) {
    return (
      <p role="status" className="rounded-md bg-amber-50 p-3 text-amber-900">
        That couldn't be understood: {answer.reason}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <ul aria-label="What was understood" className="flex flex-wrap gap-2">
        {answer.chips.map((chip) => (
          <li key={chip.name} className="rounded-full bg-gray-100 px-2 py-0.5 text-sm">
            {chip.name}: {chip.value}
          </li>
        ))}
      </ul>
      {answer.findings.length === 0 ? (
        <p role="status" className="text-gray-600">
          Nothing matches that.
        </p>
      ) : (
        <ul aria-label="Findings" className="flex flex-col gap-3">
          {answer.findings.map((finding) => (
            <FindingItem key={finding.insight.id} finding={finding} onPlay={(ms) => onPlay(finding.visit_id, ms)} />
          ))}
        </ul>
      )}
    </div>
  );
}

function FindingItem({ finding, onPlay }: { finding: Finding; onPlay: (ms: number) => void }) {
  const { insight } = finding;
  const quote = insight.quotes[0];
  const teams = insight.routes
    .map((route) => `${route.team_name}: ${route.issue_status === null ? "no issue" : STATUS_LABELS[route.issue_status]}`)
    .join(" · ");
  return (
    <li className="flex flex-col gap-1 rounded-lg border border-gray-200 p-3">
      <p className="text-xs text-gray-600">
        {finding.shop_name} ({finding.area}) · {formatDateTime(finding.visited_at)}
      </p>
      <p className="text-xs text-gray-600">{teams}</p>
      <p>{insight.summary_en}</p>
      {quote !== undefined && <QuoteLine quote={quote} onPlay={onPlay} />}
      <Link to={`/visits/${finding.visit_id}`} className="text-sm text-blue-700 underline">
        Open the visit
      </Link>
    </li>
  );
}
