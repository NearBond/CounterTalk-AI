"""scripts/transcribe_visit.py: a dry run, then the real transcribe path (batch 6), with a fake."""

from collections.abc import Callable

import pytest
from sqlalchemy import select

from countertalk import jobs, storage
from countertalk.db import session_scope
from countertalk.errors import RetryableError
from countertalk.models import Job, JobKind, JobState, VisitStatus
from scripts.transcribe_visit import main
from tests.test_transcription import (
    FakeStt,
    chunks,
    short_chunks,  # noqa: F401 - autouse here too
    transcripts,
    uploaded_visit,
)
from tests.test_visits import Org_, load, make_org


@pytest.fixture
def org() -> Org_:
    return make_org()


def job_of(visit_id: str) -> Job:
    with session_scope(read_only=True) as session:
        query = select(Job).where(Job.visit_id == visit_id, Job.kind == JobKind.TRANSCRIBE)
        return session.scalars(query).one()


def test_a_dry_run_prints_what_would_be_sent_and_sends_nothing(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = uploaded_visit(org, 15)
    fake = FakeStt()

    assert main([visit_id], stt=fake) == 0

    out = capsys.readouterr().out
    first, second = chunks(visit_id)
    seconds = (second.end_ms - first.start_ms) / 1000
    assert fake.calls == []
    assert "chunk 0: to send" in out and "chunk 1: to send" in out
    assert f"to send: {seconds:.1f} s" in out
    assert f"${seconds * 0.18 / 3600:.4f}" in out
    assert (load(visit_id).status, job_of(visit_id).state) == (
        VisitStatus.UPLOADED,
        JobState.QUEUED,
    )


def test_yes_runs_the_visits_job_and_a_retry_reuses_saved_replies(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = uploaded_visit(org, 15)
    failing = FakeStt({1: RetryableError("stt_server_error", "Speech-to-text failed")})
    assert main([visit_id, "--yes"], stt=failing) == 1
    assert job_of(visit_id).state == JobState.QUEUED  # again, after a backoff
    main([visit_id], stt=FakeStt())
    dry = capsys.readouterr().out

    retry = FakeStt()
    assert main([visit_id, "--yes"], stt=retry) == 0

    assert "chunk 0: saved reply, reused" in dry and "chunk 1: to send" in dry
    assert len(retry.calls) == 1
    assert (load(visit_id).status, job_of(visit_id).state) == (
        VisitStatus.TRANSCRIBED,
        JobState.DONE,
    )
    assert "transcribed: 4 turns" in capsys.readouterr().out


def test_a_second_copy_is_refused_while_the_job_runs(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = uploaded_visit(org, 5)
    with session_scope() as session:
        assert jobs.claim(session, "another-copy") is not None
    fake = FakeStt()

    assert main([visit_id, "--yes"], stt=fake) == 1

    assert fake.calls == []
    assert "already running" in capsys.readouterr().err


def test_no_language_bias_sends_none(org: Org_) -> None:
    visit_id = uploaded_visit(org, 5)
    fake = FakeStt()

    assert main([visit_id, "--no-language-bias", "--yes"], stt=fake) == 0

    assert fake.languages == [()]
    assert [t.language_code for t in transcripts(visit_id)] == [None]


def test_yes_without_a_key_stops_before_taking_the_job(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = uploaded_visit(org, 5)  # MODEL_API_KEY is empty in tests (conftest)

    assert main([visit_id, "--yes"]) == 1

    job = job_of(visit_id)
    assert (job.state, job.attempts) == (JobState.QUEUED, 0)
    assert "MODEL_API_KEY" in capsys.readouterr().err


def transcribed(visit_id: str) -> None:
    assert main([visit_id, "--yes"], stt=FakeStt()) == 0


def running(visit_id: str) -> None:
    with session_scope() as session:
        assert jobs.claim(session, "a-worker") is not None


@pytest.mark.parametrize("make", [transcribed, running], ids=["transcribed", "running"])
def test_a_dry_run_with_nothing_to_send_says_so_and_prepares_nothing(
    org: Org_, capsys: pytest.CaptureFixture[str], make: Callable[[str], None]
) -> None:
    visit_id = uploaded_visit(org, 5)
    make(visit_id)
    manifest = storage.chunks_manifest_path(visit_id)
    prepared_before = manifest.stat().st_mtime_ns if manifest.exists() else None
    capsys.readouterr()

    assert main([visit_id]) == 0

    assert "nothing to send" in capsys.readouterr().out
    assert (manifest.stat().st_mtime_ns if manifest.exists() else None) == prepared_before


def test_ctrl_c_queues_the_job_again_rather_than_leaving_it_running(org: Org_) -> None:
    visit_id = uploaded_visit(org, 5)

    assert main([visit_id, "--yes"], stt=FakeStt({0: KeyboardInterrupt()})) == 130

    job = job_of(visit_id)
    assert (job.state, job.attempts) == (JobState.QUEUED, 1)
    assert load(visit_id).status == VisitStatus.TRANSCRIBING
