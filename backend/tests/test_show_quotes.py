"""scripts/show_quotes.py: each quote's match and playback time, and the timing check against
a role-play's script (batch 11)."""

import json
from pathlib import Path

import pytest

from countertalk.errors import RetryableError
from countertalk.llm.base import Outcome
from scripts.show_quotes import main
from tests.test_extraction import run, transcribed
from tests.test_quotes import ASKED, PARAPHRASE, quoting
from tests.test_roles import ALL_PLACED, call, tiny_org
from tests.test_visits import Org_

BUSY = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def extracted(org: Org_) -> str:
    """A visit whose one insight has a quote matched in T1 (playback 00:04) and one unmatched."""
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, quoting((1, ASKED), (3, PARAPHRASE)), BUSY)
    return visit_id


def script(tmp_path: Path, start_ms: int) -> str:
    """The role-play's script: T1's words said from `start_ms`, 1.5 s long."""
    turn = {"role": "shopkeeper", "text": ASKED, "start_ms": start_ms, "end_ms": start_ms + 1_500}
    path = tmp_path / "rp99.turns.json"
    path.write_text(json.dumps({"turns": [turn]}), encoding="utf-8")
    return str(path)


def test_each_quote_with_its_match_playback_and_gap(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = extracted(org)

    assert main([visit_id, "--turns", script(tmp_path, 4_500)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "",
        f"{visit_id}: 2 quotes",
        f"  #0.0 exact 00:04 gap +0.5 s (fit 100)  {ASKED[:-1]}",
        "  #0.1 none",
        "",
        "Totals: 1 of 2 quotes matched",
        # Of all the quotes: one unmatched is never heard (the plan's 90% is of all quotes).
        "  heard within 3 s (gap 0 to 3 s): 1/2 (50%)",
        "  playback after the quote began (gap under -0.5 s): 0",
    ]


def test_playback_that_starts_after_the_quote_began_is_listed(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = extracted(org)

    main([visit_id, "--turns", script(tmp_path, 3_000)])

    assert capsys.readouterr().out.splitlines()[-4:] == [
        "Totals: 1 of 2 quotes matched",
        "  heard within 3 s (gap 0 to 3 s): 0/2 (0%)",
        "  playback after the quote began (gap under -0.5 s): 1",
        f"    {visit_id} #0.0",
    ]


def test_without_a_script_it_shows_the_quotes_alone(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = extracted(org)

    assert main([visit_id]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "",
        f"{visit_id}: 2 quotes",
        f"  #0.0 exact 00:04  {ASKED[:-1]}",
        "  #0.1 none",
        "",
        "Totals: 1 of 2 quotes matched",
    ]


def scripted(tmp_path: Path, *turns: tuple[str, int, int]) -> str:
    rows = [{"role": "shopkeeper", "text": t, "start_ms": a, "end_ms": b} for t, a, b in turns]
    path = tmp_path / "rp99.turns.json"
    path.write_text(json.dumps({"turns": rows}), encoding="utf-8")
    return str(path)


def test_a_quote_said_twice_in_the_script_is_placed_where_it_plays(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = extracted(org)  # its quote plays at 00:04
    turns = scripted(tmp_path, (ASKED, 0, 1_500), ("Namaste", 2_000, 3_000), (ASKED, 4_500, 6_000))

    main([visit_id, "--turns", turns])

    assert f"  #0.0 exact 00:04 gap +0.5 s (fit 100)  {ASKED[:-1]}" in (
        capsys.readouterr().out.splitlines()
    )


def test_a_quote_across_two_script_turns_is_placed_in_the_first(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Speech-to-text joined two script turns into the one quoted: it begins in the first.
    visit_id = extracted(org)
    turns = scripted(tmp_path, ("Kurkure ke do", 4_200, 5_000), ("dabbe chahiye.", 5_000, 6_000))

    main([visit_id, "--turns", turns])

    assert f"  #0.0 exact 00:04 gap +0.2 s (fit 100)  {ASKED[:-1]}" in (
        capsys.readouterr().out.splitlines()
    )


def test_several_visits_take_their_scripts_and_offsets_in_order(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = extracted(org), extracted(org)
    one = scripted(tmp_path, (ASKED, 4_500, 6_000))
    later = tmp_path / "later.turns.json"
    rows = [{"role": "shopkeeper", "text": ASKED, "start_ms": 30_500, "end_ms": 32_000}]
    later.write_text(json.dumps({"turns": rows}), encoding="utf-8")

    main([first, second, "--turns", one, str(later), "--offset-ms", "0", "-26000"])

    assert capsys.readouterr().out.splitlines()[-3:] == [
        "Totals: 2 of 4 quotes matched",
        "  heard within 3 s (gap 0 to 3 s): 2/4 (50%)",
        "  playback after the quote began (gap under -0.5 s): 0",
    ]


@pytest.mark.parametrize(
    "extra",
    [["--turns", "a.json"], ["--offset-ms", "5"]],
    ids=["one-script-for-two-visits", "offset-without-scripts"],
)
def test_scripts_and_offsets_must_go_with_the_visits(
    org: Org_, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    first, second = extracted(org), extracted(org)

    with pytest.raises(SystemExit):
        main([first, second, *extra])

    assert capsys.readouterr().out == ""  # refused before anything is shown


def test_a_bad_script_is_one_line_before_anything_is_shown(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = extracted(org), extracted(org)
    good = scripted(tmp_path, (ASKED, 4_500, 6_000))

    assert main([first, second, "--turns", good, str(tmp_path / "missing.json")]) == 1

    out, err = capsys.readouterr()
    assert (out, err) == ("", f"can't use {tmp_path / 'missing.json'}: it can't be read\n")
