"""Shows each quote of a visit with its match and when it plays: a dev tool (batch 11).

    uv run python scripts/show_quotes.py <visit_id> [<visit_id> ...]
        [--turns <rpNN.turns.json> ...] [--offset-ms N ...]

One row per extracted quote: its match (exact, fuzzy or none), the mm:ss playback starts at, and
the transcript's words. With --turns (one per visit, in the same order), each matched quote is
also placed in the role-play's script: the script turn, or two neighbouring turns joined, it
best fits (verify's normalising, then rapidfuzz partial_ratio; a tie goes to the one nearest
where it plays), and where in that text it begins, by its share of the turn. The gap is that
moment less where playback starts. A quote whose best fit is under PLACE_MIN_FIT is
unplaced: shown with its fit, and left out of the timing. Of the placed quotes, the totals count
those heard within HEARD_WITHIN_MS (a gap from 0) and list those whose playback starts more
than LATE_MS after the quote began. --offset-ms (one per visit) shifts a script for a recording
that starts later.

The fits are low across scripts: the role-plays write English words in Latin, and
speech-to-text writes them in Devanagari. Transliterating them isn't worth it for a dev check.

It prints the quotes: use it on the generated test recordings only, never on a real visit.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rapidfuzz import fuzz
from sqlalchemy import select

from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.logs import configure_logging
from countertalk.models import Evidence, Insight, Visit, parse_id
from countertalk.verify import Normalised, normalise
from scripts.compare_transcript import BadReference, Span, clock, load_reference

HEARD_WITHIN_MS = 3_000
LATE_MS = 500
# Below this a placement isn't trusted. From batch 11's K1 run (1 Nov): the one quote placed in
# the wrong turn fit at 47, and the right ones from 49 up, mostly 60-100.
PLACE_MIN_FIT = 50


@dataclass(frozen=True)
class Quote:
    insight: int
    seq: int
    match: str | None
    start_ms: int | None
    text: str | None


def load_quotes(visit_id: str) -> list[Quote] | None:
    """The extracted insights' quotes, in order; None if there's no such visit."""
    with session_scope(read_only=True) as session:
        if session.get(Visit, visit_id) is None:
            return None
        rows = session.execute(
            select(Insight.seq, Evidence.seq, Evidence.match, Evidence.start_ms)
            .add_columns(Evidence.quote_final)
            .join(Insight, Insight.id == Evidence.insight_id)
            .where(Evidence.visit_id == visit_id, Insight.extraction_id.is_not(None))
            .order_by(Insight.seq, Evidence.seq)
        )
        return [Quote(*row) for row in rows]


@dataclass(frozen=True)
class Piece:
    """A script turn, or two neighbouring ones joined (speech-to-text can merge them): its text
    normalised once, and the turns to time a position in it by."""

    turns: tuple[Span, ...]
    normalised: Normalised

    def time_at(self, char: int) -> int:
        """When the script says the character at `char` of the joined text."""
        for turn in self.turns:
            if char < len(turn.text) or turn is self.turns[-1]:
                share = min(char, len(turn.text)) / max(len(turn.text), 1)
                return round(turn.start_ms + share * (turn.end_ms - turn.start_ms))
            char -= len(turn.text) + 1  # and the space joining them
        raise AssertionError("unreachable")


def pieces(script: Sequence[Span]) -> list[Piece]:
    found = [Piece((turn,), normalise(turn.text)) for turn in script]
    for first, second in zip(script, script[1:], strict=False):
        found.append(Piece((first, second), normalise(f"{first.text} {second.text}")))
    return [piece for piece in found if piece.normalised.text]


def place(text: str, start_ms: int, script: Sequence[Piece]) -> tuple[int, float] | None:
    """Where in the mix the quote begins, by the script text it best fits, and the fit's score."""
    wanted = normalise(text).text
    best: tuple[float, int, int] | None = None
    for piece in script:
        said = piece.normalised
        if not wanted or len(wanted) > len(said.text):
            continue
        aligned = fuzz.partial_ratio_alignment(wanted, said.text)
        at = piece.time_at(said.spans[min(aligned.dest_start, len(said.spans) - 1)][0])
        nearness = -abs(at - start_ms)  # of two equal fits, the one nearest where it plays
        if best is None or (aligned.score, nearness) > (best[0], best[1]):
            best = (aligned.score, nearness, at)
    if best is None:
        return None
    score, _, at = best
    return at, score


@dataclass
class Totals:
    quotes: int = 0
    matched: int = 0
    placed: int = 0
    heard: int = 0
    late: list[str] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)


def show(
    visit_id: str, quotes: Sequence[Quote], script: list[Piece] | None, totals: Totals
) -> None:
    print(f"\n{visit_id}: {len(quotes)} quotes")
    for quote in quotes:
        totals.quotes += 1
        name = f"#{quote.insight}.{quote.seq}"
        if quote.text is None or quote.start_ms is None:
            print(f"  {name} {quote.match or '-'}")
            continue
        totals.matched += 1
        line = f"  {name} {quote.match:<5} {clock(quote.start_ms)}"
        found = place(quote.text, quote.start_ms, script) if script is not None else None
        if found is not None and found[1] < PLACE_MIN_FIT:
            totals.unplaced.append(f"{visit_id} {name} (fit {found[1]:.0f})")
            line += f" unplaced (fit {found[1]:.0f})"
        elif found is not None:
            begins, score = found
            gap = begins - quote.start_ms
            totals.placed += 1
            totals.heard += 0 <= gap <= HEARD_WITHIN_MS
            if gap < -LATE_MS:
                totals.late.append(f"{visit_id} {name}")
            line += f" gap {gap / 1000:+.1f} s (fit {score:.0f})"
        print(f"{line}  {quote.text}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="show_quotes.py")
    parser.add_argument("visit_ids", nargs="+")
    parser.add_argument(
        "--turns", type=Path, action="extend", nargs="+", help="rpNN.turns.json, one per visit"
    )
    parser.add_argument(
        "--offset-ms", type=int, action="extend", nargs="+", help="one per visit, with --turns"
    )
    args = parser.parse_args(argv)
    count = len(args.visit_ids)
    if args.turns and len(args.turns) != count:
        parser.error("give one --turns per visit, in the same order")
    if args.offset_ms and (not args.turns or len(args.offset_ms) != count):
        parser.error("--offset-ms goes with --turns, one per visit")
    visit_ids = [parse_id(raw) for raw in args.visit_ids]
    if None in visit_ids:
        parser.error("those aren't all visit IDs")
    try:
        # Everything read before anything is shown: a bad input never leaves half a report.
        quotes = [load_quotes(visit_id) for visit_id in visit_ids]  # type: ignore[arg-type]
        for visit_id, found in zip(visit_ids, quotes, strict=True):
            if found is None:
                print(f"there's no visit {visit_id}", file=sys.stderr)
                return 1
        scripts: list[list[Piece] | None] = [None] * count
        for n, path in enumerate(args.turns or []):
            offset = args.offset_ms[n] if args.offset_ms else 0
            try:
                scripts[n] = pieces(load_reference(path, offset))
            except BadReference as why:
                print(f"can't use {path}: {why}", file=sys.stderr)
                return 1
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    totals = Totals()
    for visit_id, found, script in zip(visit_ids, quotes, scripts, strict=True):
        show(visit_id, found, script, totals)  # type: ignore[arg-type]
    print(f"\nTotals: {totals.matched} of {totals.quotes} quotes matched")
    if args.turns:
        within, late = HEARD_WITHIN_MS / 1000, LATE_MS / 1000
        heard = f"{totals.heard}/{totals.placed}"
        if totals.placed:
            heard += f" ({totals.heard / totals.placed:.0%})"
        print(f"  placed in the script (fit {PLACE_MIN_FIT} or more): {totals.placed}")
        print(f"  heard within {within:g} s (gap 0 to {within:g} s): {heard}")
        print(f"  playback after the quote began (gap under -{late:g} s): {len(totals.late)}")
        for name in totals.late:
            print(f"    {name}")
        print(f"  unplaced (fit under {PLACE_MIN_FIT}): {len(totals.unplaced)}")
        for name in totals.unplaced:
            print(f"    {name}")
    return 0


if __name__ == "__main__":
    configure_logging()
    # The words are Devanagari: a Windows console's code page can't print them otherwise.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.exit(main(sys.argv[1:]))
