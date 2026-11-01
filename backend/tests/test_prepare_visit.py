"""scripts/prepare_visit.py: prepares a stored visit's audio, for the manual test (batch 5)."""

import pytest

from countertalk.models import VisitClient, new_id
from scripts import prepare_visit
from tests.test_audio import TALK_WITH_PAUSES, wav_file
from tests.test_visits import complete, make_org, new_visit, upload


def test_a_completed_visit_is_prepared_and_its_chunks_listed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    org = make_org()
    visit_id = new_visit(org, client=VisitClient.UPLOAD)
    data = wav_file(TALK_WITH_PAUSES, 4)
    for seq, start in enumerate(range(0, len(data), 64 * 1024)):
        upload(org, visit_id, seq, data[start : start + 64 * 1024])
    complete(org, visit_id, last_seq=seq, ext="wav")

    assert prepare_visit.main([visit_id]) == 0

    printed = capsys.readouterr().out
    assert "duration 4.0 s" in printed
    assert "chunk 0: 0.0 s to 4.0 s" in printed


@pytest.mark.parametrize("which", ["unknown", "recording"])
def test_a_visit_that_cant_be_prepared_is_refused_in_one_line(
    capsys: pytest.CaptureFixture[str], which: str
) -> None:
    visit_id = new_id() if which == "unknown" else new_visit(make_org())

    assert prepare_visit.main([visit_id]) == 1

    assert len(capsys.readouterr().err.strip().splitlines()) == 1
