"""scripts/export_eval_case.py: a K1 visit as an eval case (batch 13)."""

import json
from pathlib import Path

import pytest
from sqlalchemy import update

from countertalk.db import session_scope
from countertalk.models import Org, Visit, ist_date, utc_now
from scripts.export_eval_case import main
from tests.test_extraction import VISITED, answer, run, transcribed
from tests.test_roles import ALL_PLACED, VISIT, tiny_org

DRAFT = "# Planner's draft from the script (1 Nov 2026), for the user to review and edit."
# CRLF, to show line endings are kept.
KEY = "\r\n".join(
    ["# K2 for rp99: a test.", DRAFT, "# Teams.", "recording: rp99", "insights: []", ""]
)
LASTS_MS = 80_000  # the test visit's length
SCRIPT = b'{"turns": [{"role": "rep", "start_ms": 0, "end_ms": 80000, "text": "Namaste"}]}\r\n'


@pytest.fixture
def samples(tmp_path: Path) -> Path:
    folder = tmp_path / "samples"
    folder.mkdir()
    (folder / "rp99.turns.json").write_bytes(SCRIPT)
    (folder / "rp99.expected.yaml").write_bytes(KEY.encode())
    return folder


def a_visit(*, roles_saved: bool = True, lasts_ms: int = LASTS_MS) -> str:
    visit_id = transcribed(tiny_org())
    with session_scope() as session:
        session.execute(update(Visit).where(Visit.id == visit_id).values(duration_ms=lasts_ms))
    if roles_saved:
        run(visit_id, ALL_PLACED, answer())  # saves its roles, and extracts on the tiny pack
    return visit_id


def export(visit_id: str, samples: Path, to: Path, name: str = "rp99") -> int:
    return main([visit_id, name, "--samples", str(samples), "--to", str(to)])


def test_a_visit_becomes_a_case_as_the_pipeline_sees_it(
    samples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = a_visit()

    assert export(visit_id, samples, tmp_path / "cases") == 0

    folder = tmp_path / "cases" / "rp99"
    case = json.loads((folder / "turns.json").read_text(encoding="utf-8"))
    assert {k: case[k] for k in ("version", "recording", "org_pack", "visited_at")} == {
        "version": 1,
        "recording": "rp99",
        "org_pack": {"id": "tiny-demo", "version": 1},
        "visited_at": VISITED.isoformat(),
    }
    assert case["roles"]["playbook"] == {"id": "fmcg-kirana-visit", "version": 1}
    assert sorted(case["roles"]["speakers"]) == ["c0:A", "c0:B", "c0:C"]
    assert [(t["turn_id"], t["label"], t["start_ms"], t["text"]) for t in case["turns"]] == [
        (n, label, start, text) for n, (label, start, text) in enumerate(VISIT)
    ]
    # Byte for byte, line endings too; only the draft line changes, to today's date.
    assert (folder / "script_turns.json").read_bytes() == SCRIPT
    today = ist_date(utc_now())
    checked = f"# Answer key, checked {today.day} {today:%b %Y}."
    assert (folder / "expected.yaml").read_bytes() == KEY.replace(DRAFT, checked).encode()
    assert capsys.readouterr().out == f"rp99: 4 turns, 3 labels -> {folder}\n"


def test_the_case_names_the_pack_it_was_extracted_with(samples: Path, tmp_path: Path) -> None:
    visit_id = a_visit()
    with session_scope() as session:  # the org moved on since
        org_id = session.get_one(Visit, visit_id).org_id
        session.execute(update(Org).where(Org.id == org_id).values(org_pack_version=2))

    export(visit_id, samples, tmp_path)

    case = json.loads((tmp_path / "rp99" / "turns.json").read_text(encoding="utf-8"))
    assert case["org_pack"] == {"id": "tiny-demo", "version": 1}


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ("no-roles", "visit {visit}'s roles aren't saved"),
        (
            "another-recording",
            "visit {visit} isn't rp99's recording: it lasts 90 s, the script 80 s",
        ),
        ("another-key", "rp99.expected.yaml is rp98's answer key"),
        ("no-draft-line", "rp99.expected.yaml hasn't one draft header line to mark as checked"),
        ("there-already", "{to} is there already: delete it to export again"),
    ],
)
def test_what_cant_be_exported_is_one_line_and_writes_nothing(
    samples: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], change: str, why: str
) -> None:
    visit_id = a_visit(
        roles_saved=change != "no-roles",
        lasts_ms=LASTS_MS + (10_000 if change == "another-recording" else 0),
    )
    key = samples / "rp99.expected.yaml"
    if change == "another-key":
        key.write_bytes(KEY.replace("recording: rp99", "recording: rp98").encode())
    if change == "no-draft-line":
        key.write_bytes(KEY.replace(DRAFT, "# Checked.").encode())
    cases = tmp_path / "cases"
    if change == "there-already":
        (cases / "rp99").mkdir(parents=True)

    assert export(visit_id, samples, cases) == 1

    assert capsys.readouterr().err == f"can't export rp99: {why}\n".format(
        visit=visit_id, to=cases / "rp99"
    )
    left = sorted(p.name for p in cases.iterdir()) if cases.exists() else []
    assert left == (["rp99"] if change == "there-already" else [])


def test_a_name_that_isnt_rp_and_two_digits_is_refused(samples: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        export(a_visit(roles_saved=False), samples, tmp_path, name="../rp99")
