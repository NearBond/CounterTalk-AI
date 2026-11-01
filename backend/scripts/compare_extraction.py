"""Compares a visit's extracted insights with a role-play's expected ones (K2): a dev tool
(batch 10; batch 13's run_eval imports match_insights).

    uv run python scripts/compare_extraction.py <visit_id> <rpNN.expected.yaml>
        [<visit_id> <rpNN.expected.yaml> ...] [--turns <rpNN.turns.json>]

Each expected insight's phrase finds its turn in the script (an exact substring, both sides NFC),
and rpNN.turns.json (next to the expected file, or --turns for one visit) gives that turn's time
in the mix. An extracted insight matches it when one of its quotes' turns overlaps that time,
give or take SLACK_MS. They're paired one to one, the biggest overlap first.

- An expected insight left unmatched is a miss, unless it's `optional: true`.
- An extracted one left unmatched is invented. One matched to an optional insight is neither.
- For each pair: kind, category, severity, the AI's suggested team against the expected
  team(s), and the scheme. Product and competitor links wait for batch 12.
- Quotes: is each one exactly in its cited turn? (Batch 11 checks them properly.)

It prints each visit's table, then the totals. It prints quotes and summaries: use it on the
generated test recordings only, never on a real visit.
"""

import argparse
import sys
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, tuple_

from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.logs import configure_logging
from countertalk.models import Evidence, Insight, Team, Turn, Visit, parse_id
from countertalk.playbooks import read_yaml
from scripts.compare_transcript import BadReference, Span, load_reference

SLACK_MS = 2_000


@dataclass(frozen=True)
class Expected:
    number: int
    kind: str
    category: str
    severity: str
    teams: tuple[str, ...]
    scheme: str | None
    optional: bool
    phrase: str
    # The phrase's turn in the mix, or None if no turn of the script has it.
    window: tuple[int, int] | None


@dataclass(frozen=True)
class Extracted:
    seq: int
    kind: str
    category: str
    severity: str
    team: str | None
    scheme: str | None
    summary: str
    # Each quote's turn, in the visit's time; a quote citing no turn has none.
    spans: tuple[tuple[int, int], ...]
    quotes: int
    exact_quotes: int


@dataclass
class Result:
    pairs: list[tuple[Expected, Extracted]] = field(default_factory=list)
    missed: list[Expected] = field(default_factory=list)
    invented: list[Extracted] = field(default_factory=list)


def overlap(span: tuple[int, int], window: tuple[int, int]) -> int:
    return max(0, min(span[1], window[1] + SLACK_MS) - max(span[0], window[0] - SLACK_MS))


def match_insights(expected: Sequence[Expected], extracted: Sequence[Extracted]) -> Result:
    """Pairs them one to one, the biggest overlap first (module docstring)."""
    candidates = sorted(
        (
            (max(overlap(span, want.window) for span in got.spans), want.number, got.seq, want, got)
            for want in expected
            for got in extracted
            if want.window is not None and got.spans
        ),
        key=lambda c: (-c[0], c[1], c[2]),
    )
    result = Result()
    taken_expected: set[int] = set()
    taken_extracted: set[int] = set()
    for amount, _, _, want, got in candidates:
        if amount > 0 and want.number not in taken_expected and got.seq not in taken_extracted:
            result.pairs.append((want, got))
            taken_expected.add(want.number)
            taken_extracted.add(got.seq)
    result.missed = [w for w in expected if w.number not in taken_expected and not w.optional]
    result.invented = [g for g in extracted if g.seq not in taken_extracted]
    return result


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


class K2Insight(BaseModel):
    """One expected insight, as K2 writes it (batch 8's note), read strictly (R4)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    kind: str
    category: str
    severity: str
    speaker: str
    phrase: str
    skus: list[str]
    competitors: list[str]
    scheme: str | None
    teams: list[str]
    optional: bool = False


class K2File(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recording: str
    insights: list[K2Insight]


def load_expected(path: Path, script: Sequence[Span]) -> list[Expected]:
    """The expected insights, each with its phrase's turn in the mix. Raises BadReference."""
    try:
        file = K2File.model_validate(read_yaml(path))
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise BadReference(f"{where}: {first['msg']}") from None
    found = []
    for number, item in enumerate(file.insights, start=1):
        phrase = nfc(item.phrase)
        turns = [turn for turn in script if phrase in nfc(turn.text)]
        if len(turns) != 1:
            where = "no turn" if not turns else f"{len(turns)} turns; the first is used"
            print(f"  ! {path.name} #{number}: its phrase is in {where} of the script")
        found.append(
            Expected(
                number=number,
                kind=item.kind,
                category=item.category,
                severity=item.severity,
                teams=tuple(item.teams),
                scheme=item.scheme,
                optional=item.optional,
                phrase=item.phrase,
                window=(turns[0].start_ms, turns[0].end_ms) if turns else None,
            )
        )
    return found


def load_extracted(visit_id: str) -> list[Extracted] | None:
    """The visit's extracted insights, each quote's turn read from the transcript it cites."""
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        if visit is None or visit.transcript_id is None:
            return None
        teams = {team_id: key for key, team_id in session.execute(select(Team.key, Team.id))}
        cited = dict(
            ((row.transcript_id, row.turn_id), row)
            for row in session.scalars(
                select(Turn).where(
                    tuple_(Turn.transcript_id, Turn.turn_id).in_(
                        select(Evidence.transcript_id, Evidence.turn_id).where(
                            Evidence.visit_id == visit_id
                        )
                    )
                )
            )
        )
        quotes: dict[str, list[Evidence]] = {}
        for quote in session.scalars(
            select(Evidence).where(Evidence.visit_id == visit_id).order_by(Evidence.seq)
        ):
            quotes.setdefault(quote.insight_id, []).append(quote)
        query = select(Insight).where(
            Insight.visit_id == visit_id, Insight.extraction_id.is_not(None)
        )
        found = []
        for row in session.scalars(query.order_by(Insight.seq)):
            own = [(q, cited.get((q.transcript_id, q.turn_id))) for q in quotes.get(row.id, [])]
            found.append(
                Extracted(
                    seq=row.seq,
                    kind=row.kind,
                    category=row.category,
                    severity=row.severity,
                    team=teams.get(row.ai_team_id) if row.ai_team_id else None,
                    scheme=row.scheme_key,
                    summary=row.summary_en,
                    spans=tuple((t.start_ms, t.end_ms) for _, t in own if t is not None),
                    quotes=len(own),
                    exact_quotes=sum(
                        1
                        for q, t in own
                        if t is not None and q.quote_llm.strip() and nfc(q.quote_llm) in nfc(t.text)
                    ),
                )
            )
        return found


def mark(same: bool) -> str:
    return "ok" if same else "NO"


def team_agrees(want: Expected, got: Extracted) -> bool:
    return got.team in want.teams if want.teams else got.team is None


def report(name: str, result: Result, extracted: Sequence[Extracted]) -> None:
    print(
        f"\n{name}: {len(result.pairs)} matched, {len(result.missed)} missed,"
        f" {len(result.invented)} invented, of {len(extracted)} extracted"
    )
    for want, got in sorted(result.pairs, key=lambda p: p[0].number):
        optional = " (optional)" if want.optional else ""
        print(
            f"  #{want.number}{optional} {want.kind}/{want.category}/{want.severity}"
            f" -> {got.kind}/{got.category}/{got.severity}:"
            f" kind {mark(want.kind == got.kind)}, category {mark(want.category == got.category)},"
            f" severity {mark(want.severity == got.severity)},"
            f" team {got.team or '-'} {mark(team_agrees(want, got))},"
            f" scheme {got.scheme or '-'} {mark(want.scheme == got.scheme)},"
            f" quotes exact {got.exact_quotes}/{got.quotes}"
        )
        print(f"      expected: {want.phrase}")
        print(f"      got: {got.summary}")
    for want in result.missed:
        print(f"  MISSED #{want.number} {want.kind}/{want.category}/{want.severity}: {want.phrase}")
    for got in result.invented:
        print(f"  INVENTED {got.kind}/{got.category}/{got.severity}: {got.summary}")


@dataclass
class Totals:
    required: int = 0
    matched_required: int = 0
    matched_optional: int = 0
    extracted: int = 0
    pairs: int = 0
    categories: int = 0
    severities: int = 0
    teams: int = 0
    quotes: int = 0
    exact_quotes: int = 0
    invented_where_none_expected: int = 0

    def add(
        self, expected: Sequence[Expected], extracted: Sequence[Extracted], result: Result
    ) -> None:
        self.required += sum(1 for w in expected if not w.optional)
        self.matched_required += sum(1 for w, _ in result.pairs if not w.optional)
        self.matched_optional += sum(1 for w, _ in result.pairs if w.optional)
        self.extracted += len(extracted)
        self.pairs += len(result.pairs)
        self.categories += sum(w.category == g.category for w, g in result.pairs)
        self.severities += sum(w.severity == g.severity for w, g in result.pairs)
        self.teams += sum(team_agrees(w, g) for w, g in result.pairs)
        self.quotes += sum(g.quotes for g in extracted)
        self.exact_quotes += sum(g.exact_quotes for g in extracted)
        if not expected:
            self.invented_where_none_expected += len(extracted)

    def lines(self) -> list[str]:
        def share(part: int, whole: int) -> str:
            return f"{part}/{whole} ({part / whole:.0%})" if whole else f"{part}/0"

        counted = self.extracted - self.matched_optional  # an optional match is never invented
        return [
            f"recall: {share(self.matched_required, self.required)}",
            f"precision: {share(self.matched_required, counted)}",
            f"category agrees: {share(self.categories, self.pairs)}",
            f"severity agrees: {share(self.severities, self.pairs)}",
            f"team suggestion agrees: {share(self.teams, self.pairs)}",
            f"quotes exact: {share(self.exact_quotes, self.quotes)}",
            f"invented where nothing was expected: {self.invented_where_none_expected}",
        ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="compare_extraction.py")
    parser.add_argument("pairs", nargs="+", help="<visit_id> <rpNN.expected.yaml>, repeated")
    parser.add_argument("--turns", type=Path, help="rpNN.turns.json, for one visit only")
    parser.add_argument(
        "--offset-ms", type=int, default=0, help="for a recording that starts after the mix"
    )
    args = parser.parse_args(argv)
    one = len(args.pairs) == 2
    if len(args.pairs) % 2 or ((args.turns or args.offset_ms) and not one):
        parser.error("give each visit ID its expected file; --turns and --offset-ms take one")
    totals = Totals()
    try:
        for raw_id, expected_path in zip(args.pairs[::2], args.pairs[1::2], strict=True):
            visit_id = parse_id(raw_id)
            if visit_id is None:
                parser.error(f"{raw_id} isn't a visit ID")
            path = Path(expected_path)
            turns_path = args.turns or path.with_name(
                path.name.replace(".expected.yaml", ".turns.json")
            )
            extracted = load_extracted(visit_id)
            if extracted is None:
                print(f"visit {visit_id} has no transcript", file=sys.stderr)
                return 1
            try:
                script = load_reference(turns_path, args.offset_ms)
                expected = load_expected(path, script)
            except BadReference as why:
                print(f"can't use {path.name} or {turns_path.name}: {why}", file=sys.stderr)
                return 1
            result = match_insights(expected, extracted)
            report(f"{path.name.split('.')[0]} ({visit_id})", result, extracted)
            totals.add(expected, extracted, result)
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:  # an expected file that isn't valid YAML, or is missing
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1
    print("\nTotals:")
    for line in totals.lines():
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    configure_logging()
    # The words are Devanagari: a Windows console's code page can't print them otherwise.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.exit(main(sys.argv[1:]))
