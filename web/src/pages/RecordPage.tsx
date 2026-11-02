import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { RetryAlert } from "../components/RetryAlert";
import { UploadPanel } from "../components/UploadPanel";
import { messageOf } from "../lib/api";
import { countOf, formatDuration, shopLabel } from "../lib/format";
import { useElapsed, useShops } from "../lib/hooks";
import { type Recorder, startRecorder } from "../lib/recorder";
import type { QueueState, UploadQueue } from "../lib/uploadQueue";
import { finishVisit, openVisit } from "../lib/visits";

type Phase =
  | { name: "idle" }
  | { name: "starting" }
  | { name: "recording"; since: number }
  | { name: "finishing" }
  /** complete failed after its retries: the recording is safe on the server, so Try again. */
  | { name: "stuck" };

/** One recording, from Start to its visit page. Leaving the page stops all of it (§9.9, §9.10). */
interface Session {
  controller: AbortController;
  recorder?: Recorder;
  queue?: UploadQueue;
  visitId?: string;
  segments: number;
  finishing: boolean;
  /** Stop, or the microphone going away, came before the visit was ready. */
  stopRequested: boolean;
}

function stopSession(session: { current: Session | null }): void {
  const current = session.current;
  session.current = null;
  current?.controller.abort();
  current?.recorder?.abandon();
  current?.queue?.stop();
}

const BUTTON =
  "rounded-md px-4 py-2 font-medium text-white focus-visible:outline-2 focus-visible:outline-offset-2 disabled:cursor-not-allowed disabled:opacity-50";

export function RecordPage() {
  const navigate = useNavigate();
  const shops = useShops();
  const [shopId, setShopId] = useState("");
  const [phase, setPhase] = useState<Phase>({ name: "idle" });
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [queueState, setQueueState] = useState<QueueState | null>(null);
  const [uploading, setUploading] = useState(false);
  const session = useRef<Session | null>(null);
  const elapsed = useElapsed(phase.name === "recording" ? phase.since : null);
  const busy = phase.name !== "idle";
  const locked = busy || uploading;

  useEffect(() => () => stopSession(session), []);

  useEffect(() => {
    if (!locked) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [locked]);

  const fail = (current: Session, message: string) => {
    if (session.current !== current) return;
    stopSession(session);
    setPhase({ name: "idle" });
    setError(message);
  };

  const finish = async (current: Session) => {
    const { queue, visitId } = current;
    if (queue === undefined || visitId === undefined) {
      current.stopRequested = true;
      return;
    }
    if (current.finishing || session.current !== current) return;
    current.finishing = true;
    setPhase({ name: "finishing" });
    try {
      await current.recorder?.stop();
      if (current.segments === 0) {
        fail(current, "Nothing was recorded. Start again.");
        return;
      }
      await finishVisit(queue, visitId, current.segments - 1, "webm", current.controller.signal);
      if (session.current !== current) return;
      session.current = null;
      navigate(`/visits/${visitId}`);
    } catch (failure) {
      if (session.current !== current) return;
      current.finishing = false;
      if (queue.state.failed !== null) {
        fail(current, `Part of the recording couldn't be uploaded: ${queue.state.failed.message}`);
        return;
      }
      setPhase({ name: "stuck" });
      setError(messageOf(failure, "The visit couldn't be finished."));
    }
  };

  const start = async () => {
    if (busy || uploading || shopId === "") return;
    setError(null);
    setNotice(null);
    setQueueState(null);
    setPhase({ name: "starting" });
    const current: Session = { controller: new AbortController(), segments: 0, finishing: false, stopRequested: false };
    session.current = current;
    const { signal } = current.controller;
    const early: Array<[number, Blob]> = [];
    const onSegment = (seq: number, blob: Blob) => {
      current.segments = seq + 1;
      if (current.queue === undefined) early.push([seq, blob]);
      else if (current.queue.state.failed === null) current.queue.add(seq, blob);
    };
    const onInterrupted = () => {
      if (session.current !== current) return;
      setNotice("The microphone stopped. What was recorded is being sent; start a new visit to carry on.");
      void finish(current);
    };
    try {
      // The microphone first, then the visit: a browser that can't record leaves nothing behind.
      current.recorder = await startRecorder({ onSegment, onInterrupted }, signal);
      const since = Date.now(); // recording from here, while the visit is made
      const opened = await openVisit(
        shopId,
        "web",
        (state) => {
          if (session.current !== current) return;
          setQueueState(state);
          if (state.failed !== null) void finish(current);
        },
        signal,
      );
      if (session.current !== current) {
        opened.queue.stop();
        return;
      }
      current.visitId = opened.visitId;
      current.queue = opened.queue;
      for (const [seq, blob] of early) opened.queue.add(seq, blob);
      setPhase({ name: "recording", since });
      if (current.stopRequested) void finish(current);
    } catch (failure) {
      fail(current, `${messageOf(failure, "Recording couldn't start.")} Press Start recording to try again.`);
    }
  };

  const shopList = shops.data?.items ?? [];
  const waiting = queueState?.waiting ?? 0;
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-6 px-4 py-8">
      {/* Locked while busy (§9.13): leaving would stop the recording. */}
      {locked ? (
        <span aria-disabled="true" className="text-gray-400">
          Visits
        </span>
      ) : (
        <Link to="/" className="text-blue-700 underline">
          Visits
        </Link>
      )}
      <h1 className="text-2xl font-semibold">Record a visit</h1>

      <label className="flex flex-col gap-1">
        <span className="font-medium">Shop</span>
        <select
          value={shopId}
          onChange={(event) => setShopId(event.target.value)}
          disabled={locked || shops.data === undefined}
          className="rounded-md border border-gray-300 px-3 py-2"
        >
          <option value="">{shops.data === undefined ? "Loading shops…" : "Pick a shop"}</option>
          {shopList.map((shop) => (
            <option key={shop.id} value={shop.id} disabled={shop.do_not_record}>
              {shopLabel(shop)}
              {shop.do_not_record ? " (said no to recording)" : ""}
            </option>
          ))}
        </select>
      </label>
      {shops.error !== null && (
        <RetryAlert onRetry={shops.reload}>{shops.error.message}</RetryAlert>
      )}

      <section aria-labelledby="record-heading" className="flex flex-col gap-3">
        <h2 id="record-heading" className="text-lg font-semibold">
          Record here
        </h2>
        {phase.name === "idle" && (
          <button
            type="button"
            onClick={() => void start()}
            disabled={shopId === "" || uploading}
            className={`${BUTTON} self-start bg-red-700`}
          >
            Start recording
          </button>
        )}
        <p role="status" className="text-gray-700">
          {phase.name === "starting" && "Starting…"}
          {phase.name === "recording" && `Recording ${formatDuration(elapsed)}`}
          {phase.name === "finishing" && "Finishing: sending the last of the recording…"}
          {(phase.name === "recording" || phase.name === "finishing") &&
            waiting > 0 &&
            ` · ${countOf(waiting, "piece", "pieces")} waiting to send${queueState?.retrying ? " (check the connection)" : ""}`}
        </p>
        {phase.name === "recording" && (
          <button
            type="button"
            onClick={() => session.current !== null && void finish(session.current)}
            className={`${BUTTON} self-start bg-gray-900`}
          >
            Stop
          </button>
        )}
        {phase.name === "stuck" && (
          <button
            type="button"
            onClick={() => session.current !== null && void finish(session.current)}
            className={`${BUTTON} self-start bg-gray-900`}
          >
            Try again
          </button>
        )}
        {notice !== null && <p className="text-amber-800">{notice}</p>}
        {error !== null && (
          <RetryAlert>{error}</RetryAlert>
        )}
      </section>

      <UploadPanel shopId={shopId} disabled={busy} onBusyChange={setUploading} />
    </main>
  );
}
