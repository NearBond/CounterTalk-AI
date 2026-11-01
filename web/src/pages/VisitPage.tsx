import { useRef } from "react";
import { Link, useParams } from "react-router";
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

/** `/visits/:id`: the status, the audio and the transcript. Polled only while processing. */
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

  const playFrom = (turn: Turn) => {
    const player = audio.current;
    if (player === null) return;
    player.currentTime = turn.start_ms / 1000;
    player.play().catch(() => {}); // a browser that refuses to play still moved to the line
  };

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
        <p role="alert" className="text-red-700">
          {visit.error.message}{" "}
          <button type="button" onClick={visit.reload} className="underline">
            Try again
          </button>
        </p>
      )}
      {shops.error !== null && (
        <p role="alert" className="text-red-700">
          The shop names couldn't be loaded.{" "}
          <button type="button" onClick={shops.reload} className="underline">
            Try again
          </button>
        </p>
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
        <audio ref={audio} controls preload="metadata" src={audioUrl(visitId)} className="w-full">
          <track kind="captions" />
        </audio>
      )}

      {transcript.error !== null && (
        <p role="alert" className="text-red-700">
          The transcript couldn't be loaded: {transcript.error.message}{" "}
          <button type="button" onClick={transcript.reload} className="underline">
            Try again
          </button>
        </p>
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
                  onClick={() => playFrom(turn)}
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
