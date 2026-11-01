import { useEffect, useState } from "react";
import { addNote } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { useSave } from "../lib/hooks";
import { newId, saveWithRetry } from "../lib/review";
import { NOTE_MAX_CHARS, type Note } from "../types/api";
import { SaveError } from "./SaveError";

interface Props {
  visitId: string;
  notes: Note[];
  editable: boolean;
  onAdded: (note: Note) => void;
  onDirty: (dirty: boolean) => void;
}

/** The rep's notes: for the visit's record only, never sent to a team. */
export function NotesPanel({ visitId, notes, editable, onAdded, onDirty }: Props) {
  const [text, setText] = useState("");
  // Made at the first send and kept until it's added, so a retry can't add it twice.
  const [pendingId, setPendingId] = useState<string | null>(null);
  const save = useSave();
  const dirty = text.trim() !== "";
  useEffect(() => {
    onDirty(dirty);
    return () => onDirty(false);
  }, [dirty, onDirty]);

  /** Sends the note as it is now: the first time, and again behind Try again. */
  const send = () => {
    const id = pendingId ?? newId();
    setPendingId(id);
    const body = { id, text: text.trim() };
    save.run(
      (signal) => saveWithRetry((s) => addNote(visitId, body, s), signal),
      (added) => {
        setText("");
        setPendingId(null);
        onAdded(added);
      },
    );
  };

  return (
    <section aria-labelledby="notes-heading" className="flex flex-col gap-2">
      <h2 id="notes-heading" className="text-lg font-semibold">
        Notes
      </h2>
      <p className="text-sm text-gray-600">Notes are for this visit's record only. They aren't sent to any team.</p>
      {notes.length > 0 && (
        <ul className="flex flex-col gap-2">
          {notes.map((note) => (
            <li key={note.id} className="rounded-md bg-gray-50 p-2">
              <p className="text-xs text-gray-500">{formatDateTime(note.created_at)}</p>
              <p className="whitespace-pre-wrap">{note.text}</p>
            </li>
          ))}
        </ul>
      )}
      {editable && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            send();
          }}
          className="flex flex-col gap-2"
        >
          <label className="flex flex-col gap-1 text-sm">
            Add a note
            <textarea
              value={text}
              maxLength={NOTE_MAX_CHARS}
              disabled={save.saving}
              onChange={(event) => setText(event.target.value)}
              className="rounded-md border border-gray-300 p-2"
              rows={3}
            />
          </label>
          <SaveError error={save.error} onRetry={send} />
          <button
            type="submit"
            disabled={!dirty || save.saving}
            className="self-start rounded-md bg-blue-700 px-3 py-1.5 text-white disabled:opacity-50"
          >
            {save.saving ? "Adding…" : "Add note"}
          </button>
        </form>
      )}
    </section>
  );
}
