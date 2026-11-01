import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { messageOf } from "../lib/api";
import type { QueueState, UploadQueue } from "../lib/uploadQueue";
import { byteRanges, extOf, finishVisit, openVisit } from "../lib/visits";
import { ALLOWED_EXTS, type RecordingExt } from "../types/api";

interface Props {
  shopId: string;
  /** The recorder is busy: nothing here can start. */
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
}

/** What's needed to finish an upload whose complete failed: Try again redoes only that. */
interface Sending {
  controller: AbortController;
  queue?: UploadQueue;
  visitId?: string;
  lastSeq: number;
  ext: RecordingExt;
}

/** A phone recording's file, sent as byte ranges the server joins back together (BRIEF §2). */
export function UploadPanel({ shopId, disabled, onBusyChange }: Props) {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<QueueState | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [canRetry, setCanRetry] = useState(false);
  const sending = useRef<Sending | null>(null);

  useEffect(
    () => () => {
      sending.current?.controller.abort();
      sending.current?.queue?.stop();
      sending.current = null;
    },
    [],
  );

  const setWorking = (working: boolean) => {
    setBusy(working);
    onBusyChange(working);
  };

  const finish = async (current: Sending) => {
    const { queue, visitId } = current;
    if (queue === undefined || visitId === undefined || disabled) return;
    setWorking(true);
    setError(null);
    setCanRetry(false);
    try {
      await finishVisit(queue, visitId, current.lastSeq, current.ext, current.controller.signal);
      if (sending.current !== current) return;
      sending.current = null;
      navigate(`/visits/${visitId}`);
    } catch (failure) {
      if (sending.current !== current) return;
      setWorking(false);
      setError(messageOf(failure, "The upload couldn't be finished."));
      // Only complete can be tried again: a refused piece means choosing the file again.
      setCanRetry(queue.state.failed === null);
    }
  };

  const upload = async () => {
    if (file === null || busy || disabled || shopId === "") return;
    setCanRetry(false); // a new attempt: an earlier upload's Try again no longer applies
    const ext = extOf(file.name);
    if (ext === null) {
      setError(`That file type can't be sent. Use one of: ${ALLOWED_EXTS.join(", ")}.`);
      return;
    }
    const ranges = byteRanges(file.size);
    if (ranges.length === 0) {
      setError("That file is empty.");
      return;
    }
    sending.current?.queue?.stop();
    const current: Sending = { controller: new AbortController(), lastSeq: ranges.length - 1, ext };
    sending.current = current;
    setWorking(true);
    setError(null);
    setCanRetry(false);
    setProgress(null);
    setTotal(ranges.length);
    try {
      const opened = await openVisit(
        shopId,
        "upload",
        (state) => sending.current === current && setProgress(state),
        current.controller.signal,
      );
      if (sending.current !== current) {
        opened.queue.stop();
        return;
      }
      current.visitId = opened.visitId;
      current.queue = opened.queue;
      ranges.forEach(([start, end], seq) => opened.queue.add(seq, file.slice(start, end)));
    } catch (failure) {
      if (sending.current !== current) return;
      sending.current = null;
      setWorking(false);
      setError(`${messageOf(failure, "The upload couldn't start.")} Press Send to try again.`);
      return;
    }
    await finish(current);
  };

  const tryAgain = () => {
    const current = sending.current;
    if (current !== null) void finish(current);
  };

  return (
    <section aria-labelledby="upload-heading" className="flex flex-col gap-3 border-t border-gray-200 pt-6">
      <h2 id="upload-heading" className="text-lg font-semibold">
        Or send a recording from the phone
      </h2>
      <label className="flex flex-col gap-1">
        <span className="font-medium">Recording file</span>
        <input
          type="file"
          accept={ALLOWED_EXTS.map((ext) => `.${ext}`).join(",")}
          disabled={busy || disabled}
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          className="text-sm"
        />
      </label>
      <button
        type="button"
        onClick={() => void upload()}
        disabled={file === null || busy || disabled || shopId === ""}
        className="self-start rounded-md bg-gray-900 px-4 py-2 font-medium text-white focus-visible:outline-2 focus-visible:outline-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
      >
        Send
      </button>
      <p role="status" className="text-gray-700">
        {busy && progress !== null && `Sent ${progress.uploaded} of ${total} ${total === 1 ? "piece" : "pieces"}`}
        {busy && progress?.retrying && " (check the connection)"}
      </p>
      {error !== null && (
        <p role="alert" className="text-red-700">
          {error}{" "}
          {canRetry && (
            <button type="button" onClick={tryAgain} disabled={disabled} className="underline disabled:opacity-50">
              Try again
            </button>
          )}
        </p>
      )}
    </section>
  );
}
