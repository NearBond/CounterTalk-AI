"""scripts/compare_extraction.py: matching extracted insights to K2's expected ones (batch 10).

match_insights is what batch 13's run_eval imports, so it's tested on its own, table-driven.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select

from countertalk import routing
from countertalk.db import session_scope
from countertalk.models import Insight, Team
from scripts.compare_extraction import (
    SLACK_MS,
    UNCHECKED,
    UNVERIFIED,
    Expected,
    Extracted,
    Result,
    Totals,
    link_outcome,
    main,
    match_insights,
    routing_outcome,
)
from tests.test_extraction import answer, insight, run, transcribed
from tests.test_roles import ALL_PLACED, tiny_org


def want(number: int, window: tuple[int, int] | None, *, optional: bool = False) -> Expected:
    return Expected(number, "problem", "supply", "high", ("field",), None, optional, "p", window)


def got(seq: int, *spans: tuple[int, int]) -> Extracted:
    return Extracted(seq, "problem", "supply", "high", "field", None, "s", spans, ("exact",), True)


def pairs(result: Result) -> list[tuple[int, int]]:
    return sorted((w.number, g.seq) for w, g in result.pairs)


@pytest.mark.parametrize(
    ("expected", "extracted", "matched", "missed", "invented"),
    [
        # Overlapping its turn: a match.
        ([want(1, (10_000, 12_000))], [got(0, (11_000, 11_500))], [(1, 0)], [], []),
        # Within the slack either side, still a match; just past it, none.
        ([want(1, (10_000, 12_000))], [got(0, (12_000 + SLACK_MS - 1, 20_000))], [(1, 0)], [], []),
        ([want(1, (10_000, 12_000))], [got(0, (12_000 + SLACK_MS, 20_000))], [], [1], [0]),
        # One to one, the biggest overlap first: #1 takes 0, so #2 gets 1.
        (
            [want(1, (10_000, 14_000)), want(2, (13_000, 20_000))],
            [got(0, (10_000, 14_000)), got(1, (13_500, 15_000))],
            [(1, 0), (2, 1)],
            [],
            [],
        ),
        # Any of an insight's quotes may be the one that overlaps.
        ([want(1, (50_000, 52_000))], [got(0, (1_000, 2_000), (50_500, 51_000))], [(1, 0)], [], []),
        # An optional insight: matched, it's neither a miss nor an invention; left, not a miss.
        ([want(1, (10_000, 12_000), optional=True)], [got(0, (11_000, 11_500))], [(1, 0)], [], []),
        ([want(1, (10_000, 12_000), optional=True)], [], [], [], []),
        # A phrase found in no turn can't match; a quote citing no turn can't either.
        ([want(1, None)], [got(0, (11_000, 11_500))], [], [1], [0]),
        ([want(1, (10_000, 12_000))], [got(0)], [], [1], [0]),
    ],
    ids=[
        "overlap",
        "in-slack",
        "past-slack",
        "one-to-one",
        "any-quote",
        "optional-matched",
        "optional-left",
        "phrase-in-no-turn",
        "quote-in-no-turn",
    ],
)
def test_matching(
    expected: list[Expected],
    extracted: list[Extracted],
    matched: list[tuple[int, int]],
    missed: list[int],
    invented: list[int],
) -> None:
    result = match_insights(expected, extracted)

    assert pairs(result) == matched
    assert [w.number for w in result.missed] == missed
    assert [g.seq for g in result.invented] == invented


def test_totals_leave_optional_matches_out_of_precision() -> None:
    expected = [
        want(1, (0, 1_000)),
        want(2, (5_000, 6_000), optional=True),
        want(3, (9_000, 9_500)),
    ]
    extracted = [got(0, (0, 500)), got(1, (5_000, 5_500)), got(2, (20_000, 21_000))]
    totals = Totals()

    totals.add(expected, extracted, match_insights(expected, extracted))

    assert totals.lines()[:2] == [
        "recall: 1/2 (50%) of expected (optional aside)",
        "precision: 1/2 (50%) of extracted (optional aside)",
    ]


def test_a_visit_against_its_expected_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    org = tiny_org()
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, answer(insight(), insight(category="praise", kind="praise")))
    # The script's turns: the phrase is in the turn at 4.0–4.9 s, where the insight's quote is.
    turn = {"role": "shopkeeper", "text": "Kurkure ke do dabbe chahiye."}
    script = {"turns": [turn | {"start_ms": 4_000, "end_ms": 4_900}]}
    (tmp_path / "rp99.turns.json").write_text(json.dumps(script), encoding="utf-8")
    (tmp_path / "rp99.expected.yaml").write_text(
        "recording: rp99\n"
        "insights:\n"
        "  - {kind: product_request, category: demand, severity: medium, speaker: shopkeeper,\n"
        "     phrase: do dabbe, skus: [crunchy], competitors: [], scheme: null, teams: [field]}\n",
        encoding="utf-8",
    )

    assert main([visit_id, str(tmp_path / "rp99.expected.yaml")]) == 0

    none = "extra 0, wrong 0"
    assert capsys.readouterr().out.splitlines() == [
        "",
        f"rp99 ({visit_id}): 1 matched, 0 missed, 1 invented, of 2 extracted",
        "  #1 product_request/demand/medium -> product_request/demand/low: kind ok, category ok,"
        " severity NO, team field ok, quotes exact 1, fuzzy 0, none 0",
        # "Kurkure" isn't in the tiny pack: no link, and a review.
        "      links: sku - missing, competitor - agree, scheme - agree;"
        " routed field agree (expected field)",
        "      expected: do dabbe",
        "      got: The shop wants two boxes.",
        "  INVENTED praise/praise/low: The shop wants two boxes.",
        f"  sku link, of 1 matched: agree 0, missing 1, {none}",
        f"  competitor link, of 1 matched: agree 1, missing 0, {none}",
        f"  scheme link, of 1 matched: agree 1, missing 0, {none}",
        "  routed teams, of 1 matched: agree 1, differ 0, not routed: unverified 0",
        "  alias reviews: 1 (own 'Kurkure')",
        "",
        "Totals:",
        "  recall: 1/1 (100%) of expected (optional aside)",
        "  precision: 1/2 (50%) of extracted (optional aside)",
        "  category agrees: 1/1 (100%) of matched",
        "  severity agrees: 0/1 (0%) of matched",
        "  team suggestion agrees: 1/1 (100%) of matched",
        "  quotes: exact 2, fuzzy 0, none 0",
        "  insights verified: 2/2 (100%) of extracted",
        "  invented where nothing was expected: 0",
        f"  sku link, of 1 matched: agree 0, missing 1, {none}",
        f"  competitor link, of 1 matched: agree 1, missing 0, {none}",
        f"  scheme link, of 1 matched: agree 1, missing 0, {none}",
        "  routed teams, of 1 matched: agree 1, differ 0, not routed: unverified 0",
        "  routed teams agree: 1/1 (100%) of those the rule routed or triaged",
        "  alias reviews: 1 (own 1, competitor 0)",
    ]


def k2_file(tmp_path: Path, teams: str) -> str:
    # rp99's script and expected file: one demand insight at the quote's turn (4.0 s).
    turn = {"role": "shopkeeper", "text": "do dabbe", "start_ms": 4_000, "end_ms": 4_900}
    (tmp_path / "rp99.turns.json").write_text(json.dumps({"turns": [turn]}), encoding="utf-8")
    path = tmp_path / "rp99.expected.yaml"
    path.write_text(
        "recording: rp99\ninsights:\n  - {kind: product_request, category: demand, severity: low,"
        " speaker: shopkeeper, phrase: do dabbe, skus: [], competitors: [], scheme: null,"
        f" {teams}}}\n",
        encoding="utf-8",
    )
    return str(path)


def test_a_person_s_later_change_isnt_the_rule_s_routing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    org = tiny_org()
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, answer())  # demand: the AI's field
    with session_scope() as session:
        [insight_id] = session.scalars(select(Insight.id).where(Insight.visit_id == visit_id))
        money = session.scalar(
            select(Team.id).where(Team.org_id == org.org_id, Team.key == "money")
        )
        routing.also_send(session, org.org_id, insight_id, money, org.rep_id)
    expected = k2_file(tmp_path, "teams: [field]")

    main([visit_id, expected])

    out = capsys.readouterr().out.splitlines()
    assert out[3].endswith("routed field agree (expected field)")


@pytest.mark.parametrize(
    ("wanted", "got", "outcome"),
    [
        ((), None, "agree"),
        (("kurkure",), "kurkure", "agree"),
        (("kurkure", "lays"), "lays", "agree"),  # one link: any of K2's agrees
        (("kurkure",), None, "missing"),
        ((), "kurkure", "extra"),
        (("kurkure",), "lays", "wrong"),
    ],
)
def test_a_link_against_k2s(wanted: tuple[str, ...], got: str | None, outcome: str) -> None:
    assert link_outcome(wanted, got) == outcome


@pytest.mark.parametrize(
    ("teams", "routes", "verified", "outcome"),
    [
        (("finance", "area_manager"), ("area_manager", "finance"), True, "agree"),
        ((), (), True, "agree"),  # triage, as expected
        (("finance",), ("finance", "area_manager"), True, "differ"),
        (("finance",), (), True, "differ"),
        (("finance",), None, False, UNVERIFIED),  # never routed, so neither
        ((), None, True, UNCHECKED),  # extracted before batch 12: never routed either
    ],
)
def test_routed_teams_against_k2s(
    teams: tuple[str, ...], routes: tuple[str, ...] | None, verified: bool, outcome: str
) -> None:
    expected = replace(want(1, (0, 1)), teams=teams)
    extracted = replace(got(0, (0, 1)), routes=routes, verified=verified)

    assert routing_outcome(expected, extracted) == outcome


def test_links_never_made_are_unchecked_not_scored() -> None:
    expected = [want(1, (0, 1_000))]
    before = replace(got(0, (0, 500)), linked=False)
    totals = Totals()

    totals.add(expected, [before], match_insights(expected, [before]))

    assert totals.lines()[8:12] == [
        "sku link, of 1 matched: agree 0, missing 0, extra 0, wrong 0, unchecked 1",
        "competitor link, of 1 matched: agree 0, missing 0, extra 0, wrong 0, unchecked 1",
        "scheme link, of 1 matched: agree 0, missing 0, extra 0, wrong 0, unchecked 1",
        "routed teams, of 1 matched: agree 0, differ 0, not routed: unverified 0, unchecked 1",
    ]


@pytest.mark.parametrize(
    ("line", "why"),
    [
        ("teams: field", "insights.0.teams: Input should be a valid list"),
        ("optional: 'no'", "insights.0.optional: Input should be a valid boolean"),
        ("note: extra", "insights.0.note: Extra inputs are not permitted"),
    ],
    ids=["teams-not-a-list", "optional-not-a-bool", "unknown-field"],
)
def test_an_expected_file_is_read_strictly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], line: str, why: str
) -> None:
    visit_id = transcribed(tiny_org())
    (tmp_path / "rp99.turns.json").write_text(json.dumps({"turns": []}), encoding="utf-8")
    item = "kind: k, category: c, severity: s, speaker: x, phrase: p, skus: [], competitors: []"
    fields = {"teams": "teams: []", "scheme": "scheme: null"}
    fields[line.split(":")[0]] = line
    body = ", ".join([item, *fields.values()])
    (tmp_path / "rp99.expected.yaml").write_text(
        f"recording: rp99\ninsights:\n  - {{{body}}}\n", encoding="utf-8"
    )

    assert main([visit_id, str(tmp_path / "rp99.expected.yaml")]) == 1

    assert capsys.readouterr().err == f"can't use rp99.expected.yaml or rp99.turns.json: {why}\n"


def test_an_offset_moves_the_script_to_a_recording_that_starts_later(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = transcribed(tiny_org())
    run(visit_id, ALL_PLACED, answer())  # its quote's turn is at 4.0 s in the recording
    turn = {"role": "shopkeeper", "text": "do dabbe", "start_ms": 30_000, "end_ms": 31_000}
    (tmp_path / "rp99.turns.json").write_text(json.dumps({"turns": [turn]}), encoding="utf-8")
    (tmp_path / "rp99.expected.yaml").write_text(
        "recording: rp99\ninsights:\n  - {kind: product_request, category: demand, severity: low,"
        " speaker: shopkeeper, phrase: do dabbe, skus: [], competitors: [], scheme: null,"
        " teams: [field]}\n",
        encoding="utf-8",
    )
    expected = str(tmp_path / "rp99.expected.yaml")

    main([visit_id, expected])
    without = capsys.readouterr().out.splitlines()
    main([visit_id, expected, "--offset-ms", "-26000"])
    shifted = capsys.readouterr().out.splitlines()

    recall = [line for line in without + shifted if line.startswith("  recall")]
    assert recall == [
        "  recall: 0/1 (0%) of expected (optional aside)",
        "  recall: 1/1 (100%) of expected (optional aside)",
    ]


def test_a_quote_never_checked_is_counted_apart_from_one_that_failed() -> None:
    # Extracted before batch 11: no match stored, which isn't "none".
    expected = [want(1, (0, 1_000))]
    unchecked = Extracted(
        0, "problem", "supply", "high", "field", None, "s", ((0, 500),), (None,), False
    )
    totals = Totals()

    totals.add(expected, [unchecked], match_insights(expected, [unchecked]))

    assert totals.lines()[5] == "quotes: exact 0, fuzzy 0, none 0, unchecked 1"
