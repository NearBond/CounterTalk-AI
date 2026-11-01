import { useCallback, useEffect, useState } from "react";
import { getReview } from "../lib/api";
import { useLeaveWarning, usePolling, useSave } from "../lib/hooks";
import { confirmReview } from "../lib/review";
import type { Insight, Note, Review, Turn, Visit } from "../types/api";
import { AddFinding } from "./AddFinding";
import { InsightCard } from "./InsightCard";
import { NotesPanel } from "./NotesPanel";
import { SaveError } from "./SaveError";

interface Props {
  visitId: string;
  turns: Turn[];
  lang?: string;
  /** Plays the page's one shared player from this moment. */
  onPlay: (ms: number) => void;
  /** Whether any edit, finding or note is unsaved: the page's own links ask before leaving. */
  onDirty: (dirty: boolean) => void;
  onConfirmed: (visit: Visit) => void;
}

/** The report, tasks and insights of a visit in review or confirmed, and the rep's changes. */
export function ReviewPanel({ visitId, turns, lang, onPlay, onDirty, onConfirmed }: Props) {
  const loaded = usePolling((signal) => getReview(visitId, signal), { key: visitId, shouldPoll: () => false });
  // The rep's changes, kept with the load they were made to; a fresh load (after confirm) wins.
  const [local, setLocal] = useState<{ from: Review; review: Review } | null>(null);
  const review = loaded.data === undefined ? null : local?.from === loaded.data ? local.review : loaded.data;
  const [dirtyParts, setDirtyParts] = useState<ReadonlySet<string>>(new Set());
  const markDirty = useCallback((part: string, dirty: boolean) => {
    setDirtyParts((parts) => {
      if (parts.has(part) === dirty) return parts;
      const next = new Set(parts);
      if (dirty) next.add(part);
      else next.delete(part);
      return next;
    });
  }, []);
  const dirty = dirtyParts.size > 0;
  useLeaveWarning(dirty);
  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty, onDirty]);
  const confirm = useSave();
  const findingDirty = useCallback((value: boolean) => markDirty("finding", value), [markDirty]);
  const noteDirty = useCallback((value: boolean) => markDirty("note", value), [markDirty]);

  if (loaded.error !== null) {
    return (
      <p role="alert" className="text-red-700">
        The review couldn't be loaded: {loaded.error.message}{" "}
        <button type="button" onClick={loaded.reload} className="underline">
          Try again
        </button>
      </p>
    );
  }
  if (review === null) {
    return (
      <p role="status" className="text-gray-600">
        Loading the review…
      </p>
    );
  }

  /** Each change from the latest state, never from one a save started with: two saves that
   * overlap both land. */
  const update = (change: (current: Review) => Review) => {
    const from = loaded.data;
    if (from === undefined) return;
    setLocal((previous) => ({ from, review: change(previous?.from === from ? previous.review : from) }));
  };
  const editable = review.visit.status === "review";
  const codebook = { kinds: review.kinds, categories: review.categories, severities: review.severities };
  const replace = (saved: Insight) =>
    update((r) => ({ ...r, insights: r.insights.map((found) => (found.id === saved.id ? saved : found)) }));
  const drop = (insightId: string) => update((r) => ({ ...r, insights: r.insights.filter((found) => found.id !== insightId) }));
  const add = (added: Insight) =>
    update((r) => ({ ...r, insights: r.insights.some((found) => found.id === added.id) ? r.insights : [...r.insights, added] }));
  const addNote = (note: Note) =>
    update((r) => ({ ...r, notes: r.notes.some((found) => found.id === note.id) ? r.notes : [...r.notes, note] }));
  const finish = () =>
    confirm.run(
      (signal) => confirmReview(visitId, signal),
      (visit) => {
        update((r) => ({ ...r, visit })); // read-only at once
        onConfirmed(visit);
        loaded.reload(); // the insights now visible, from the server
      },
    );

  return (
    <div className="flex flex-col gap-6">
      {review.report !== null && (
        <section aria-labelledby="report-heading" className="flex flex-col gap-1">
          <h2 id="report-heading" className="text-lg font-semibold">
            Report
          </h2>
          <p>{review.report.summary_en}</p>
          {review.report.next_visit_date !== null && (
            <p className="text-sm text-gray-600">Next visit: {review.report.next_visit_date}</p>
          )}
          {/* None for a visit extracted before order items were (batch 14), so an empty list
              isn't shown as "no order". */}
          {review.order_items.length > 0 && (
            <>
              <h3 className="mt-2 font-medium">Order</h3>
              <ul aria-label="Order" className="flex flex-col gap-1">
                {review.order_items.map((item) => {
                  const amount = [item.quantity, item.unit].filter((part) => part !== null).join(" ");
                  return (
                    <li key={item.seq} className="text-sm">
                      <span lang={lang}>{item.name}</span>
                      {amount !== "" && ` · ${amount}`}
                      {item.sku !== null && <span className="text-gray-600"> · Product: {item.sku}</span>}
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </section>
      )}

      {review.tasks.length > 0 && (
        <section aria-labelledby="tasks-heading" className="flex flex-col gap-1">
          <h2 id="tasks-heading" className="text-lg font-semibold">
            Tasks
          </h2>
          <ul className="flex flex-col gap-1">
            {review.tasks.map((task) => (
              <li key={task.seq} className="text-sm">
                {task.description_en}{" "}
                <span className="text-gray-600">
                  ({task.owner}
                  {task.due !== null && `, by ${task.due}`})
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section aria-labelledby="insights-heading" className="flex flex-col gap-2">
        <h2 id="insights-heading" className="text-lg font-semibold">
          Findings
        </h2>
        {review.insights.length === 0 && <p className="text-gray-600">Nothing was raised on this visit.</p>}
        <ul className="flex flex-col gap-3">
          {review.insights.map((insight) => (
            <InsightCard
              key={insight.id}
              insight={insight}
              codebook={codebook}
              editable={editable}
              lang={lang}
              onPlay={onPlay}
              onSaved={replace}
              onDeleted={drop}
              onDirty={markDirty}
            />
          ))}
        </ul>
      </section>

      {editable && turns.length > 0 && (
        <AddFinding
          visitId={visitId}
          turns={turns}
          speakerRoles={review.speaker_roles}
          codebook={codebook}
          onAdded={add}
          onDirty={findingDirty}
        />
      )}

      <NotesPanel visitId={visitId} notes={review.notes} editable={editable} onAdded={addNote} onDirty={noteDirty} />

      {editable ? (
        <section className="flex flex-col gap-2">
          <SaveError error={confirm.error} onRetry={confirm.retry} />
          {dirty && <p className="text-sm text-gray-600">Save or cancel your changes first.</p>}
          <button
            type="button"
            disabled={dirty || confirm.saving}
            onClick={finish}
            className="rounded-md bg-green-700 px-4 py-2 font-medium text-white disabled:opacity-50"
          >
            {confirm.saving ? "Confirming…" : "Confirm visit"}
          </button>
          <p className="text-xs text-gray-600">
            Confirming sends the findings to their teams. It can't be changed afterwards.
          </p>
        </section>
      ) : (
        <p className="rounded-md bg-green-50 p-3 text-green-900">
          Confirmed: the findings went to their teams ({review.insights.filter((i) => i.visible).length} of{" "}
          {review.insights.length}, the ones found in the recording). It can't be changed now.
        </p>
      )}
      {review.insights.some((insight) => !insight.verified) && editable && (
        <p className="text-xs text-gray-600">
          Greyed-out findings couldn't be found in the recording: they're never sent to a team.
        </p>
      )}
    </div>
  );
}
