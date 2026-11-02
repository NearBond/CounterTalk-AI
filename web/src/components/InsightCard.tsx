import { useEffect, useState } from "react";
import { ApiError, deleteInsight, patchInsight } from "../lib/api";
import { useSave } from "../lib/hooks";
import { STATUS_LABELS } from "../lib/issues";
import { changesOf, type Draft, draftOf, labelOf, saveWithRetry } from "../lib/review";
import { type Choice, type Insight, SUMMARY_MAX_CHARS } from "../types/api";
import { ChoiceSelect } from "./ChoiceSelect";
import { QuoteLine } from "./QuoteLine";
import { SaveError } from "./SaveError";

export interface Codebook {
  kinds: Choice[];
  categories: Choice[];
  severities: Choice[];
}

interface Props {
  insight: Insight;
  codebook: Codebook;
  /** False once the visit is confirmed: no edit controls. */
  editable: boolean;
  /** The transcript's main language, so a quote's script renders well. */
  lang?: string;
  onPlay: (ms: number) => void;
  onSaved: (insight: Insight) => void;
  onDeleted: (insightId: string) => void;
  /** Whether it has edits not yet saved: the page warns before leaving. */
  onDirty: (insightId: string, dirty: boolean) => void;
}

/** One insight: what it is, the shopkeeper's words with ▶, its teams, and its edits. */
export function InsightCard({ insight, codebook, editable, lang, onPlay, onSaved, onDeleted, onDirty }: Props) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const save = useSave();
  const remove = useSave();
  const busy = save.saving || remove.saving;
  const change = draft === null ? {} : changesOf(insight, draft);
  const dirty = Object.keys(change).length > 0;
  useEffect(() => {
    onDirty(insight.id, dirty);
    return () => onDirty(insight.id, false);
  }, [insight.id, dirty, onDirty]);

  /** Sends the form as it is now: the first time, and again behind Try again. */
  const send = () => {
    if (!dirty) {
      setDraft(null);
      return;
    }
    save.run(
      (signal) => saveWithRetry((s) => patchInsight(insight.id, change, s), signal),
      (saved) => {
        setDraft(null);
        onSaved(saved);
      },
    );
  };
  const askToDelete = () => {
    if (!window.confirm("Delete this finding? It won't be sent to any team.")) return;
    remove.run(
      async (signal) => {
        try {
          await saveWithRetry((s) => deleteInsight(insight.id, s), signal);
        } catch (failure) {
          // A retry after a delete whose answer was lost: it's gone already.
          if (!(failure instanceof ApiError && failure.code === "insight_not_found")) throw failure;
        }
      },
      () => onDeleted(insight.id),
    );
  };
  const teams = insight.routes.map((route) => route.team_name).join(", ");

  return (
    <li
      // A summary's citation links here (batch 15b): the page focuses it, so it's outlined.
      id={`insight-${insight.id}`}
      tabIndex={-1}
      className={`flex scroll-mt-4 flex-col gap-2 rounded-lg border p-3 focus:ring-2 focus:ring-blue-600 focus:outline-none ${
        insight.verified ? "border-gray-200" : "border-dashed border-gray-300 bg-gray-50 text-gray-500"
      }`}
    >
      <p className="text-xs font-medium tracking-wide text-gray-600 uppercase">
        {labelOf(codebook.kinds, insight.kind)} · {labelOf(codebook.categories, insight.category)} ·{" "}
        {labelOf(codebook.severities, insight.severity)}
        {insight.added_by_rep && " · added by you"}
      </p>
      {!insight.verified && <p className="text-sm font-medium">Couldn't find this in the recording</p>}

      {draft === null ? (
        <p>{insight.summary_en}</p>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            send();
          }}
          className="flex flex-col gap-2"
        >
          <label className="flex flex-col gap-1 text-sm">
            Summary
            <textarea
              value={draft.summary_en}
              maxLength={SUMMARY_MAX_CHARS}
              disabled={busy}
              onChange={(event) => setDraft({ ...draft, summary_en: event.target.value })}
              className="rounded-md border border-gray-300 p-2"
              rows={3}
            />
          </label>
          <ChoiceSelect
            label="Category"
            choices={codebook.categories}
            value={draft.category}
            disabled={busy}
            onChange={(category) => setDraft({ ...draft, category })}
          />
          <ChoiceSelect
            label="Severity"
            choices={codebook.severities}
            value={draft.severity}
            disabled={busy}
            onChange={(severity) => setDraft({ ...draft, severity })}
          />
          <p className="text-xs text-gray-600">A new category or severity sends it to that one's team.</p>
          <SaveError error={save.error} onRetry={send} />
          <div className="flex gap-3">
            <button
              type="submit"
              disabled={busy || draft.summary_en.trim() === ""}
              className="rounded-md bg-blue-700 px-3 py-1.5 text-white disabled:opacity-50"
            >
              {save.saving ? "Saving…" : "Save"}
            </button>
            <button type="button" disabled={busy} onClick={() => setDraft(null)} className="underline">
              Cancel
            </button>
          </div>
        </form>
      )}

      {insight.quotes.length > 0 && (
        <ul className="flex flex-col gap-1">
          {insight.quotes.map((quote) => (
            <li key={quote.seq}>
              <QuoteLine quote={quote} lang={lang} onPlay={onPlay} />
            </li>
          ))}
        </ul>
      )}

      {(insight.sku !== null || insight.competitor !== null || insight.scheme_key !== null) && (
        <p className="text-sm text-gray-600">
          {[
            insight.sku !== null && `Product: ${insight.sku}`,
            insight.competitor !== null && `Competitor: ${insight.competitor}`,
            insight.scheme_key !== null && `Scheme: ${insight.scheme_key}`,
          ]
            .filter(Boolean)
            .join(" · ")}
        </p>
      )}
      {insight.verified && (editable || insight.routes.length === 0) && (
        <p className="text-sm text-gray-600">{teams === "" ? "No team yet: it goes to triage" : `Sent to ${teams}`}</p>
      )}
      {insight.verified && !editable && insight.routes.length > 0 && (
        // Confirmed: each team's issue status and its answer, for the rep to tell the shop. A
        // route confirmed before issues existed shows its team alone.
        <ul aria-label="Teams" className="flex flex-col gap-1 text-sm">
          {insight.routes.map((route) => (
            <li key={route.slot}>
              <span className="font-medium">{route.team_name}</span>
              {route.issue_status !== null && ` · ${STATUS_LABELS[route.issue_status]}`}
              {route.resolution_note !== null && (
                <span className="block text-gray-700">Answer: {route.resolution_note}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {!insight.verified && editable && (
        <p className="text-sm">If it was said, add it yourself from the transcript.</p>
      )}

      {draft === null && <SaveError error={remove.error} onRetry={remove.retry} />}
      {editable && draft === null && (
        <div className="flex gap-3 text-sm">
          {insight.verified && (
            <button type="button" disabled={busy} onClick={() => setDraft(draftOf(insight))} className="underline">
              Edit
            </button>
          )}
          <button type="button" disabled={busy} onClick={askToDelete} className="text-red-700 underline">
            {remove.saving ? "Deleting…" : "Delete"}
          </button>
        </div>
      )}
    </li>
  );
}
