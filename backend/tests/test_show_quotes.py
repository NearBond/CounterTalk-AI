"""scripts/show_quotes.py: each quote's match and playback time, and the timing check against
a role-play's script (batch 11)."""

import json
from pathlib import Path

import pytest

from countertalk.errors import RetryableError
from countertalk.llm.base import Outcome
from scripts.show_quotes import PLACE_MIN_FIT, main
from tests.test_extraction import run, transcribed
from tests.test_quotes import ASKED, PARAPHRASE, quoting
from tests.test_roles import ALL_PLACED, call, tiny_org
from tests.test_visits import Org_

BUSY = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))
WORDS = ASKED[:-1]  # as matched: the full stop is punctuation
PLACED = f"  placed in the script (fit {PLACE_MIN_FIT} or more)"
UNPLACED = f"  unplaced (fit under {PLACE_MIN_FIT})"
HEARD = "  heard within 3 s (gap 0 to 3 s)"
LATE = "  playback after the quote began (gap under -0.5 s)"


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def extracted(org: Org_) -> str:
    """A visit whose one insight has a quote matched in T1 (said at 00:04, played from 00:02.5)
    and one unmatched."""
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, quoting((1, ASKED), (3, PARAPHRASE)), BUSY)
    return visit_id


def scripted(tmp_path: Path, *turns: tuple[str, int, int], name: str = "rp99") -> str:
    rows = [{"role": "shopkeeper", "text": t, "start_ms": a, "end_ms": b} for t, a, b in turns]
    path = tmp_path / f"{name}.turns.json"
    path.write_text(json.dumps({"turns": rows}), encoding="utf-8")
    return str(path)


def test_each_quote_with_its_match_playback_and_gap(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = extracted(org)

    assert main([visit_id, "--turns", scripted(tmp_path, (ASKED, 4_500, 6_000))]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "",
        f"{visit_id}: 2 quotes",
        f"  #0.0 exact 00:02 gap +2.0 s (fit 100)  {WORDS}",
        "  #0.1 none",
        "",
        "Totals: 1 of 2 quotes matched",
        f"{PLACED}: 1",
        f"{HEARD}: 1/1 (100%)",  # of the placed quotes
        f"{LATE}: 0",
        f"{UNPLACED}: 0",
    ]


def test_playback_that_starts_after_the_quote_began_is_listed(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = extracted(org)

    main([visit_id, "--turns", scripted(tmp_path, (ASKED, 1_000, 2_500))])

    assert capsys.readouterr().out.splitlines()[-5:] == [
        f"{PLACED}: 1",
        f"{HEARD}: 0/1 (0%)",
        f"{LATE}: 1",
        f"    {visit_id} #0.0",
        f"{UNPLACED}: 0",
    ]


def test_a_quote_the_script_hasnt_got_is_unplaced_and_left_out_of_the_share(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = extracted(org)
    script = scripted(tmp_path, ("Aaj mausam bahut achha hai yaar", 0, 9_000))

    main([visit_id, "--turns", script])

    out = capsys.readouterr().out.splitlines()
    fit = int(out[2].split("(fit ")[1].split(")")[0])
    assert fit < PLACE_MIN_FIT
    assert out[2] == f"  #0.0 exact 00:02 unplaced (fit {fit})  {WORDS}"
    assert out[-6:] == [
        "Totals: 1 of 2 quotes matched",
        f"{PLACED}: 0",
        f"{HEARD}: 0/0",
        f"{LATE}: 0",
        f"{UNPLACED}: 1",
        f"    {visit_id} #0.0 (fit {fit})",
    ]


def test_without_a_script_it_shows_the_quotes_alone(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = extracted(org)

    assert main([visit_id]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "",
        f"{visit_id}: 2 quotes",
        f"  #0.0 exact 00:02  {WORDS}",
        "  #0.1 none",
        "",
        "Totals: 1 of 2 quotes matched",
    ]


def test_a_quote_said_twice_in_the_script_is_placed_nearest_where_it_plays(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = extracted(org)
    turns = scripted(tmp_path, (ASKED, 0, 1_500), ("Namaste", 2_000, 3_000), (ASKED, 4_500, 6_000))

    main([visit_id, "--turns", turns])

    assert capsys.readouterr().out.splitlines()[2] == (
        f"  #0.0 exact 00:02 gap +2.0 s (fit 100)  {WORDS}"
    )


def test_a_quote_across_two_script_turns_is_placed_in_the_first(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Speech-to-text joined two script turns into the one quoted: it begins in the first.
    visit_id = extracted(org)
    turns = scripted(tmp_path, ("Kurkure ke do", 4_200, 5_000), ("dabbe chahiye.", 5_000, 6_000))

    main([visit_id, "--turns", turns])

    assert capsys.readouterr().out.splitlines()[2] == (
        f"  #0.0 exact 00:02 gap +1.7 s (fit 100)  {WORDS}"
    )


def test_several_visits_take_their_scripts_and_offsets_in_order(
    org: Org_, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = extracted(org), extracted(org)
    one = scripted(tmp_path, (ASKED, 4_500, 6_000), name="rp98")
    later = scripted(tmp_path, (ASKED, 30_500, 32_000), name="rp99")

    main([first, second, "--turns", one, later, "--offset-ms", "0", "-26000"])

    assert capsys.readouterr().out.splitlines()[-5:] == [
        "Totals: 2 of 4 quotes matched",
        f"{PLACED}: 2",
        f"{HEARD}: 2/2 (100%)",
        f"{LATE}: 0",
        f"{UNPLACED}: 0",
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
