import { formatDuration } from "../lib/format";
import type { Quote } from "../types/api";

interface Props {
  quote: Quote;
  /** The transcript's main language, so a quote's script renders well. */
  lang?: string;
  onPlay: (ms: number) => void;
}

/** The shopkeeper's words, and ▶ from where they were said when they were found: every quote in
 * the app is shown this way (R1, batch 15). */
export function QuoteLine({ quote, lang, onPlay }: Props) {
  const startMs = quote.start_ms;
  return (
    <span className="flex items-start gap-2">
      {startMs !== null && (
        <button
          type="button"
          aria-label={`Play from ${formatDuration(startMs)}`}
          onClick={() => onPlay(startMs)}
          className="shrink-0 rounded-full bg-gray-100 px-2 py-0.5 text-sm"
        >
          ▶
        </button>
      )}
      <q lang={lang} className="italic">
        {quote.text}
      </q>
    </span>
  );
}
