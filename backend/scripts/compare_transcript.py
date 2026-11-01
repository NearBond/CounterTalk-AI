"""Compares a visit's transcript with a role-play's reference turns: a dev tool (batch 6).

    uv run python scripts/compare_transcript.py <visit_id> <rpNN.turns.json> [--offset-ms N]

It prints:
- speaker separation: each chunk's labels (c0:A, …) mapped to the role they overlap most in
  time, and the share of speech time that lands on the right role;
- English: the reference writes English words in Latin script, so how many of them came back in
  Latin, and how many words came back in each script;
- the turns around each chunk cut, then 10 turns side by side.

--offset-ms shifts the reference, for a recording that starts later than the mix (a browser
capture, say). It prints the words, so use it on test recordings only, never on a real visit.
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from countertalk import storage
from countertalk.db import session_scope
from countertalk.models import Turn, Visit, parse_id

LATIN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
DEVANAGARI = re.compile(r"[ऀ-ॿ]+")
CUT_WINDOW_MS = 4_000
SIDE_BY_SIDE = 10


@dataclass(frozen=True)
class Span:
    """A turn: a role in the reference, a label (c0:A) in the transcript."""

    label: str
    start_ms: int
    end_ms: int
    text: str


def overlap(a: Span, b: Span) -> int:
    return max(0, min(a.end_ms, b.end_ms) - max(a.start_ms, b.start_ms))


def map_labels(turns: list[Span], reference: list[Span]) -> dict[str, str]:
    """Each label to the reference role it overlaps most. A label is one chunk's (c0:A), so
    each chunk is mapped on its own; a label that overlaps nothing maps to nothing."""
    totals: dict[str, Counter[str]] = defaultdict(Counter)
    for turn in turns:
        for ref in reference:
            if ms := overlap(turn, ref):
                totals[turn.label][ref.label] += ms
    return {label: counts.most_common(1)[0][0] for label, counts in totals.items()}


def merged(spans: list[Span]) -> list[Span]:
    """The spans' union, so time two of them share (cross-talk) counts once."""
    union: list[Span] = []
    for span in sorted(spans, key=lambda s: s.start_ms):
        if union and span.start_ms <= union[-1].end_ms:
            last = union.pop()
            span = Span(last.label, last.start_ms, max(last.end_ms, span.end_ms), "")
        union.append(span)
    return union


def speaker_share(
    turns: list[Span], reference: list[Span], mapping: dict[str, str]
) -> tuple[int, int]:
    """(ms on the mapped role, ms overlapping any reference turn), cross-talk counted once."""
    speech = merged(reference)
    by_role = {role: merged([r for r in reference if r.label == role]) for role in mapping.values()}
    right = total = 0
    for turn in turns:
        total += sum(overlap(turn, span) for span in speech)
        right += sum(overlap(turn, span) for span in by_role.get(mapping.get(turn.label, ""), []))
    return right, total


def clock(ms: int) -> str:
    return f"{ms // 60_000:02d}:{ms // 1000 % 60:02d}"


def show(span: Span, prefix: str) -> str:
    return f"  {prefix} {clock(span.start_ms)} {span.label:<10} {span.text}"


def load_reference(path: Path, offset_ms: int) -> list[Span]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Span(t["role"], t["start_ms"] + offset_ms, t["end_ms"] + offset_ms, t["text"])
        for t in data["turns"]
    ]


def load_transcript(visit_id: str) -> list[Span] | None:
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        if visit is None or visit.transcript_id is None:
            return None
        rows = session.scalars(
            select(Turn).where(Turn.transcript_id == visit.transcript_id).order_by(Turn.turn_id)
        )
        return [Span(t.speaker_label, t.start_ms, t.end_ms, t.text) for t in rows]


def cuts(visit_id: str) -> list[int]:
    manifest = json.loads(storage.chunks_manifest_path(visit_id).read_text(encoding="utf-8"))
    return [chunk["end_ms"] for chunk in manifest["chunks"][:-1]]


def report(visit_id: str, turns: list[Span], reference: list[Span]) -> None:
    mapping = map_labels(turns, reference)
    right, total = speaker_share(turns, reference, mapping)
    print("Speakers, each chunk's labels by overlap:")
    for label in sorted({t.label for t in turns}):
        print(f"  {label} -> {mapping.get(label, '(overlaps nothing)')}")
    share = right / total if total else 0
    print(f"  speech time on the right role: {share:.1%} of {total / 1000:.0f} s")

    english = Counter(w.lower() for r in reference for w in LATIN.findall(r.text))
    latin = Counter(w.lower() for t in turns for w in LATIN.findall(t.text))
    found = sum((english & latin).values())
    devanagari = sum(len(DEVANAGARI.findall(t.text)) for t in turns)
    print("English:")
    print(f"  the reference's English words: {sum(english.values())}, back in Latin: {found}")
    print(f"  transcript words in Latin: {sum(latin.values())}, in Devanagari: {devanagari}")

    for cut in cuts(visit_id):
        window = Span("", cut - CUT_WINDOW_MS, cut + CUT_WINDOW_MS, "")
        print(f"Around the cut at {clock(cut)}:")
        for ref in reference:
            if overlap(ref, window):
                print(show(ref, "ref"))
        for turn in turns:
            if overlap(turn, window):
                print(show(turn, "stt"))

    count = min(SIDE_BY_SIDE, len(reference))
    picks = sorted({round(i * (len(reference) - 1) / max(count - 1, 1)) for i in range(count)})
    print(f"{len(picks)} turns side by side:")
    for index in picks:
        ref = reference[index]
        print(show(ref, "ref"))
        for turn in turns:
            if overlap(turn, ref):
                print(show(turn, "stt"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="compare_transcript.py")
    parser.add_argument("visit_id")
    parser.add_argument("reference", type=Path, help="a role-play's rpNN.turns.json")
    parser.add_argument("--offset-ms", type=int, default=0)
    args = parser.parse_args(argv)
    visit_id = parse_id(args.visit_id)
    if visit_id is None:
        parser.error("that isn't a visit ID")
    turns = load_transcript(visit_id)
    if turns is None:
        print(f"visit {visit_id} has no transcript", file=sys.stderr)
        return 1
    report(visit_id, turns, load_reference(args.reference, args.offset_ms))
    return 0


if __name__ == "__main__":
    # The words are Devanagari: a Windows console's code page can't print them otherwise.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.exit(main(sys.argv[1:]))
