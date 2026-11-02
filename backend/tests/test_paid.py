"""scripts/_paid.py's paid_run: every way out of a paid run prints what was paid (R8)."""

import pytest
from sqlalchemy.exc import OperationalError

from scripts import _paid
from tests.test_extraction import answer, run, transcribed
from tests.test_roles import ALL_PLACED, tiny_org


def test_a_run_s_calls_are_printed_on_the_way_out(capsys: pytest.CaptureFixture[str]) -> None:
    visit_id = transcribed(tiny_org())

    with _paid.paid_run({visit_id: 0}, heading="Paid:") as paid:
        run(visit_id, ALL_PLACED, answer())

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Paid:"
    assert [line.split(",")[0] for line in out[1:3]] == [
        "call 1: roles ok",
        "call 2: extraction ok",
    ]
    assert out[3] == f"cost: ${paid.usd(visit_id):.4f}"


def test_ctrl_c_is_stopped_once_what_was_paid_is_printed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    visit_id = transcribed(tiny_org())

    with pytest.raises(_paid.Stopped), _paid.paid_run({visit_id: 0}):
        raise KeyboardInterrupt

    assert capsys.readouterr().out == "cost: $0.0000\n"


def test_a_database_that_doesnt_answer_is_one_line(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(key: str, seq: int, *, summary: bool = False) -> list[object]:
        raise OperationalError("SELECT", {}, Exception("gone"))

    monkeypatch.setattr(_paid.llm_calls, "since", down)

    with pytest.raises(RuntimeError), _paid.paid_run({"v": 0}):
        raise RuntimeError("the run's own failure")  # still the one raised

    assert capsys.readouterr().err == (
        "what was paid can't be read: the database didn't answer (llm_calls has it)\n"
    )
