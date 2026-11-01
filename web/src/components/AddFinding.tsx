import { useEffect, useState } from "react";
import { addInsight } from "../lib/api";
import { formatDuration } from "../lib/format";
import { useSave } from "../lib/hooks";
import { newId, SHOPKEEPER, saveWithRetry } from "../lib/review";
import { type Insight, SUMMARY_MAX_CHARS, type Turn } from "../types/api";
import { ChoiceSelect } from "./ChoiceSelect";
import type { Codebook } from "./InsightCard";
import { SaveError } from "./SaveError";

interface Props {
  visitId: string;
  turns: Turn[];
  speakerRoles: Record<string, string>;
  codebook: Codebook;
  onAdded: (insight: Insight) => void;
  onDirty: (dirty: boolean) => void;
}

const SNIPPET_CHARS = 60;

/** A finding the AI missed: the rep picks the transcript line where it was said, and that line
 * becomes its quote, exact and playable. */
export function AddFinding({ visitId, turns, speakerRoles, codebook, onAdded, onDirty }: Props) {
  const empty = {
    turn: "",
    kind: codebook.kinds[0]?.key ?? "",
    category: codebook.categories[0]?.key ?? "",
    severity: codebook.severities[0]?.key ?? "",
    summary: "",
  };
  const [form, setForm] = useState(empty);
  // Made at the first send and kept until it's added, so a retry can't add it twice.
  const [pendingId, setPendingId] = useState<string | null>(null);
  const save = useSave();
  const dirty = form.turn !== "" || form.summary.trim() !== "";
  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty, onDirty]);

  /** Sends the form as it is now: the first time, and again behind Try again. */
  const send = () => {
    const id = pendingId ?? newId();
    setPendingId(id);
    const body = {
      id,
      turn_id: Number(form.turn),
      kind: form.kind,
      category: form.category,
      severity: form.severity,
      summary_en: form.summary.trim(),
    };
    save.run(
      (signal) => saveWithRetry((s) => addInsight(visitId, body, s), signal),
      (added) => {
        setForm(empty);
        setPendingId(null);
        onAdded(added);
      },
    );
  };
  const ready = form.turn !== "" && form.summary.trim() !== "";
  // Only the shopkeeper's lines: theirs are the words that become insights.
  const lines = turns.filter((turn) => speakerRoles[turn.speaker_label] === SHOPKEEPER);

  return (
    <section aria-labelledby="add-heading" className="flex flex-col gap-2">
      <h2 id="add-heading" className="text-lg font-semibold">
        Add a finding the AI missed
      </h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          send();
        }}
        className="flex flex-col gap-2"
      >
        <label className="flex flex-col gap-1 text-sm">
          Where the shopkeeper said it
          <select
            value={form.turn}
            disabled={save.saving}
            onChange={(event) => setForm({ ...form, turn: event.target.value })}
            className="rounded-md border border-gray-300 p-2"
          >
            <option value="">Pick a line of the transcript</option>
            {lines.map((turn) => (
                <option key={turn.turn_id} value={String(turn.turn_id)}>
                  {formatDuration(turn.start_ms)} ·{" "}
                  {turn.text.length > SNIPPET_CHARS ? `${turn.text.slice(0, SNIPPET_CHARS)}…` : turn.text}
                </option>
              ))}
          </select>
        </label>
        {lines.length === 0 && (
          <p className="text-sm text-gray-600">
            No line here is marked as the shopkeeper's, so there's none to quote. If the speakers
            look wrong, the visit can be processed again.
          </p>
        )}
        <ChoiceSelect
          label="Kind"
          choices={codebook.kinds}
          value={form.kind}
          disabled={save.saving}
          onChange={(kind) => setForm({ ...form, kind })}
        />
        <ChoiceSelect
          label="Category"
          choices={codebook.categories}
          value={form.category}
          disabled={save.saving}
          onChange={(category) => setForm({ ...form, category })}
        />
        <ChoiceSelect
          label="Severity"
          choices={codebook.severities}
          value={form.severity}
          disabled={save.saving}
          onChange={(severity) => setForm({ ...form, severity })}
        />
        <label className="flex flex-col gap-1 text-sm">
          Summary, in English
          <textarea
            value={form.summary}
            maxLength={SUMMARY_MAX_CHARS}
            disabled={save.saving}
            onChange={(event) => setForm({ ...form, summary: event.target.value })}
            className="rounded-md border border-gray-300 p-2"
            rows={2}
          />
        </label>
        <SaveError error={save.error} onRetry={send} />
        <button
          type="submit"
          disabled={!ready || save.saving}
          className="self-start rounded-md bg-blue-700 px-3 py-1.5 text-white disabled:opacity-50"
        >
          {save.saving ? "Adding…" : "Add finding"}
        </button>
      </form>
    </section>
  );
}
