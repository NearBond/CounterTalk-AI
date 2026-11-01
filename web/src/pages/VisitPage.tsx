import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useBlocker, useParams } from "react-router";
import { ReviewPanel } from "../components/ReviewPanel";
import { RetryAlert } from "../components/RetryAlert";
import { StatusBadge } from "../components/StatusBadge";
import { audioUrl, getTranscript, getVisit } from "../lib/api";
import { formatDateTime, formatDuration, shopLabel } from "../lib/format";
import { usePolling, useShops } from "../lib/hooks";
import type { Turn } from "../types/api";

// One colour per speaker label, in the order they first speak. Until batch 9 decides roles, the
// chips show speech-to-text's own labels (c0:A, c0:B, …).
const CHIP_COLOURS = [
  "bg-sky-100 text-sky-900",
  "bg-violet-100 text-violet-900",
  "bg-emerald-100 text-emerald-900",
  "bg-orange-100 text-orange-900",
  "bg-pink-100 text-pink-900",
  "bg-gray-200 text-gray-900",
];

function chipColours(turns: Turn[]): Map<string, string> {
  const colours = new Map<string, string>();
  for (const turn of turns) {
    if (!colours.has(turn.speaker_label)) {
      colours.set(turn.speaker_label, CHIP_COLOURS[colours.size % CHIP_COLOURS.length] ?? "");
    }
  }
  return colours;
}

/** `/visits/:id`: the status, the audio, the review (batch 14) and the transcript. Polled only
 * while processing. */
export function VisitPage() {
  const { visitId = "" } = useParams();
  const audio = useRef<HTMLAudioElement>(null);
  const visit = usePolling((signal) => getVisit(visitId, signal), {
    key: visitId,
    shouldPoll: (value) => value.processing,
  });
  // A visit's duration is saved with its transcript, so it says whether there is one: a failed
  // visit that has a transcript still shows it.
  const transcribed = visit.data?.duration_ms != null;
  const transcript = usePolling((signal) => getTranscript(visitId, signal), {
    key: transcribed ? visitId : null,
    shouldPoll: () => false,
    skip: !transcribed,
  });
  const shops = useShops();
  const shop = shops.data?.items.find((item) => item.id === visit.data?.shop_id);

  // A recording that can't be loaded (its file lost, say) says so, under the player: ▶ never
  // fails silently. The player's error event sets it, for this visit only; Try again loads the
  // recording afresh.
  const [unplayableFor, setUnplayableFor] = useState<string | null>(null);
  const unplayable = unplayableFor === visitId;
  const retry = useRef<HTMLButtonElement>(null);
  // The page's one player: a quote's ▶ and a transcript line both start it, one at a time.
  const playAt = useCallback(
    (ms: number) => {
      const player = audio.current;
      if (player === null) return;
      if (player.error !== null) {
        setUnplayableFor(visitId);
        retry.current?.focus(); // the message, read out again, and in view
        return;
      }
      player.currentTime = ms / 1000;
      player.play().catch(() => {}); // a browser that refuses to play still moved to the moment
    },
    [visitId],
  );
  const [dirty, setDirty] = useState(false);
  // Any navigation away with unsaved changes asks first: a link, or the back button (§9.9).
  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (window.confirm("Leave without saving your changes?")) blocker.proceed();
    else blocker.reset();
  }, [blocker]);
  const reviewing = visit.data?.status === "review" || visit.data?.status === "confirmed";

  const turns = transcript.data?.turns ?? [];
  // The main language the transcript was heard in ("hi" of "hi,en"), so Devanagari renders well.
  const lang = transcript.data?.language_code?.split(",")[0] || undefined;
  const colours = chipColours(turns);
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-5 px-4 py-8">
      <Link to="/" className="text-blue-700 underline">
        Visits
      </Link>
      <h1 className="text-2xl font-semibold">{shop === undefined ? "Visit" : shopLabel(shop)}</h1>

      {visit.error !== null && (
        <RetryAlert onRetry={visit.reload}>{visit.error.message}</RetryAlert>
      )}
      {shops.error !== null && (
        <RetryAlert onRetry={shops.reload}>The shop names couldn't be loaded.</RetryAlert>
      )}
      {visit.data === undefined && visit.error === null && (
        <p role="status" className="text-gray-600">
          Loading…
        </p>
      )}

      {visit.data !== undefined && (
        <section className="flex flex-col gap-2">
          <StatusBadge status={visit.data.status} />
          <p className="text-sm text-gray-600">
            {formatDateTime(visit.data.created_at)} · {formatDuration(visit.data.duration_ms)}
          </p>
          {visit.data.status === "failed" && visit.data.failure_reason !== null && (
            <p className="rounded-md bg-red-50 p-3 text-red-800">{visit.data.failure_reason}</p>
          )}
          {visit.data.processing && <p className="text-gray-600">Working on it: this page updates by itself.</p>}
        </section>
      )}

      {transcribed && (
        // Seekable: the server answers Range requests (batch 6).
        <audio
          ref={audio}
          controls
          preload="metadata"
          src={audioUrl(visitId)}
          onError={() => setUnplayableFor(visitId)}
          className="w-full"
        >
          <track kind="captions" />
        </audio>
      )}
      {unplayable && (
        <RetryAlert
          retryRef={retry}
          onRetry={() => {
            setUnplayableFor(null);
            audio.current?.load(); // a failure again sets the message again
          }}
        >
          The recording can't be played right now. Everything else on this page still works.
        </RetryAlert>
      )}

      {reviewing && (
        <ReviewPanel
          visitId={visitId}
          turns={turns}
          lang={lang}
          onPlay={playAt}
          onDirty={setDirty}
          onConfirmed={visit.reload}
        />
      )}

      {transcript.error !== null && (
        <RetryAlert onRetry={transcript.reload}>
          The transcript couldn't be loaded: {transcript.error.message}
        </RetryAlert>
      )}
      {turns.length > 0 && (
        <section aria-labelledby="transcript-heading" className="flex flex-col gap-3">
          <h2 id="transcript-heading" className="text-lg font-semibold">
            Transcript
          </h2>
          <ul className="flex flex-wrap gap-2" aria-label="Speakers">
            {[...colours].map(([label, colour]) => (
              <li key={label} className={`rounded-full px-2 py-0.5 text-xs font-medium ${colour}`}>
                {label}
              </li>
            ))}
          </ul>
          <ol className="flex flex-col gap-1">
            {turns.map((turn) => (
              <li key={turn.turn_id}>
                <button
                  type="button"
                  onClick={() => playAt(turn.start_ms)}
                  className="flex w-full items-start gap-2 rounded-md p-2 text-left hover:bg-gray-50 focus-visible:outline-2"
                >
                  <span className="shrink-0 text-xs text-gray-500 tabular-nums">{formatDuration(turn.start_ms)}</span>
                  <span className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${colours.get(turn.speaker_label)}`}>
                    {turn.speaker_label}
                  </span>
                  <span lang={lang}>{turn.text}</span>
                </button>
              </li>
            ))}
          </ol>
        </section>
      )}
    </main>
  );
}
