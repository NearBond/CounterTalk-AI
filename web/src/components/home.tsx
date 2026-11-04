import { countOf } from "../lib/format";

/* The homes' shared pieces (batches F3 and F4): v2's look for the rep's, the manager's and the
 * team member's home, each a page of stat cards, then sections of SHOWN_FIRST with View all. */

// What each list shows before its View all (N) (the user, 4 Nov).
export const SHOWN_FIRST = 3;

export const FOCUS = "focus-visible:outline-2 focus-visible:outline-offset-2";
// v2's primary button, as one big card: marigold, an ink edge and a hard ink shadow that lifts.
export const RECORD =
  "flex min-h-16 w-full items-center gap-4 rounded-[10px] border-[1.5px] border-ink bg-marigold px-4 py-3 text-lg" +
  " font-semibold text-ink shadow-[3px_3px_0_var(--color-ink)] transition hover:-translate-x-px hover:-translate-y-px" +
  " hover:shadow-[4px_4px_0_var(--color-ink)] active:translate-x-0.5 active:translate-y-0.5" +
  " active:shadow-[1px_1px_0_var(--color-ink)] focus-visible:outline-2 focus-visible:outline-offset-4" +
  " motion-reduce:transition-none sm:w-auto sm:min-w-72 sm:shrink-0";
// v2's secondary button: an ink outline on the card.
export const SECONDARY =
  "inline-flex min-h-11 shrink-0 items-center justify-center rounded-[10px] border-[1.5px] border-ink px-5" +
  ` font-semibold text-ink transition-colors hover:bg-paper-2 motion-reduce:transition-none ${FOCUS}`;
export const STAT =
  "flex h-full min-h-11 items-center gap-4 rounded-lg border border-line bg-card p-4 transition hover:border-ink" +
  ` hover:shadow-[3px_3px_0_var(--color-ink)] motion-reduce:transition-none sm:flex-col sm:items-start sm:gap-2 ${FOCUS}`;
export const QUIET_LINK = `inline-flex min-h-11 items-center underline underline-offset-4 ${FOCUS}`;
export const H2 = "text-xl font-semibold text-ink sm:text-2xl";
export const EMPTY = "rounded-lg border border-dashed border-line-strong p-4 text-ink-2";

/** A list's "View all (N)": shown only past SHOWN_FIRST; it opens the rest in place. */
export function ViewAll({
  count,
  open,
  controls,
  onToggle,
}: {
  count: number;
  open: boolean;
  controls: string;
  onToggle: (open: boolean) => void;
}) {
  if (count <= SHOWN_FIRST) return null;
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      onClick={() => onToggle(!open)}
      className={`${SECONDARY} self-start`}
    >
      {open ? "Show fewer" : `View all (${count})`}
    </button>
  );
}

/** A stat card's big figure and the plain words after it. */
export interface FigureProps {
  /** "12", or "–" while it loads. */
  figure: string;
  words: string;
  /** Kept on one line after the words: "· 1 fixed". */
  tail?: string;
  /** Whether the figure is read out with the words, as one sentence: "12 visits waiting…". */
  said: boolean;
}

/** A count as a stat card says it. At 0 or while it loads the words say it all ("No visits
 * waiting…"), and the figure is only shown: never read out as a wrong number. */
export function stat(
  count: number | undefined,
  wording: { one: string; many: string; none: string; loading: string; tail?: string },
): FigureProps {
  if (count === undefined) return { figure: "–", words: wording.loading, said: false };
  if (count === 0) return { figure: "0", words: wording.none, said: false };
  // countOf leads with the count: shown big here, apart from its words.
  const words = countOf(count, wording.one, wording.many);
  return { figure: String(count), words: words.slice(String(count).length + 1), tail: wording.tail, said: true };
}

export function Figure({ figure, words, tail, said }: FigureProps) {
  return (
    <>
      <span
        aria-hidden={said ? undefined : true}
        className="min-w-14 shrink-0 text-center font-display text-4xl leading-none font-semibold text-ink tabular-nums sm:min-w-0 sm:text-left"
      >
        {figure}
      </span>{" "}
      <span className="min-w-0 flex-1 leading-snug wrap-break-word text-ink-2">
        {words}
        {tail !== undefined && (
          <>
            {" "}
            <span className="whitespace-nowrap">{tail}</span>
          </>
        )}
      </span>
      <span aria-hidden="true" className="text-ink-3 sm:hidden">
        →
      </span>
    </>
  );
}
