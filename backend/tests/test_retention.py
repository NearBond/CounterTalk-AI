"""Retention (batch 17): a confirmed visit's audio goes after AUDIO_RETENTION_DAYS, files only once
the mark is committed (§9.3), never an unconfirmed visit's (§9.7); and old jobs go too."""

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from countertalk import retention, storage, visits, worker
from countertalk.config import get_settings
from countertalk.db import session_scope
from countertalk.models import Job, JobKind, JobState, Visit, VisitStatus, utc_now
from tests.conftest import give_transcript, sign_in
from tests.test_visits import Org_, load, make_org, move_to, new_visit

NOW = utc_now()


def recorded(org: Org_, status: str = VisitStatus.CONFIRMED, days_ago: int = 31) -> str:
    """A visit with a recording on disk, in this status: confirmed days_ago if it's confirmed."""
    visit_id = new_visit(org)
    give_transcript(visit_id)
    steps = {
        VisitStatus.RECORDING: (),
        VisitStatus.REVIEW: (
            VisitStatus.UPLOADED,
            VisitStatus.TRANSCRIBING,
            VisitStatus.TRANSCRIBED,
            VisitStatus.EXTRACTING,
            VisitStatus.REVIEW,
        ),
    }
    steps[VisitStatus.CONFIRMED] = (*steps[VisitStatus.REVIEW], VisitStatus.CONFIRMED)
    with session_scope() as session:
        session.execute(update(Visit).where(Visit.id == visit_id).values(completed_at=NOW))
    move_to(org, visit_id, *steps[status])
    if status == VisitStatus.CONFIRMED:
        when = NOW - timedelta(days=days_ago)
        with session_scope() as session:
            session.execute(update(Visit).where(Visit.id == visit_id).values(confirmed_at=when))
    for path in audio_files(visit_id) + [storage.stt_reply_path(visit_id, 0)]:
        storage.write_file(path, b"bytes")
    return visit_id


def audio_files(visit_id: str) -> list[Path]:
    return [
        storage.segment_path(visit_id, 0),
        storage.source_path(visit_id, "webm"),
        storage.prepared_path(visit_id),
        storage.chunk_path(visit_id, 0),
        storage.chunks_manifest_path(visit_id),
        storage.playback_path(visit_id),
    ]


def kept(visit_id: str) -> list[str]:
    return sorted(p.name for p in audio_files(visit_id) if p.exists())


@pytest.fixture
def org() -> Org_:
    return make_org()


def test_a_visit_confirmed_long_enough_ago_loses_its_audio_and_keeps_its_words(org: Org_) -> None:
    old, recent = recorded(org, days_ago=31), recorded(org, days_ago=29)

    swept = retention.run(now=NOW)

    assert (swept.visits, swept.files) == (1, 6)
    assert (load(old).audio_deleted_at, load(recent).audio_deleted_at) == (NOW, None)
    assert kept(old) == []
    assert len(kept(recent)) == 6
    # The transcript and speech-to-text's replies stay.
    assert load(old).transcript_id is not None
    assert storage.stt_reply_path(old, 0).exists()


@pytest.mark.parametrize(
    "status", [VisitStatus.RECORDING, VisitStatus.REVIEW], ids=["recording", "review"]
)
def test_a_visit_not_confirmed_is_never_touched(org: Org_, status: str) -> None:
    visit_id = recorded(org, status)
    with session_scope() as session:  # however old it is
        old = NOW - timedelta(days=400)
        session.execute(
            update(Visit).where(Visit.id == visit_id).values(created_at=old, updated_at=old)
        )

    swept = retention.run(now=NOW)

    assert (swept.visits, load(visit_id).audio_deleted_at, len(kept(visit_id))) == (0, None, 6)


def test_files_go_only_once_the_mark_is_committed(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded(org)
    seen: list[object] = []
    remove = storage.remove_audio

    def watched(found: str) -> int:
        with session_scope(read_only=True) as session:  # another transaction: only what's committed
            seen.append(session.get_one(Visit, found).audio_deleted_at)
        return remove(found)

    monkeypatch.setattr(storage, "remove_audio", watched)

    retention.run(now=NOW)

    assert seen == [NOW]


def test_a_run_that_stopped_before_its_files_is_finished_by_the_next(org: Org_) -> None:
    visit_id = recorded(org)
    with session_scope() as session:  # marked and committed yesterday, then that run stopped
        yesterday = NOW - timedelta(days=1)
        session.execute(
            update(Visit).where(Visit.id == visit_id).values(audio_deleted_at=yesterday)
        )

    swept = retention.run(now=NOW)

    assert (swept.visits, swept.files, kept(visit_id)) == (0, 6, [])


def test_with_no_days_a_visit_confirmed_now_loses_its_audio(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    visit_id = recorded(org, days_ago=0)
    monkeypatch.setenv("AUDIO_RETENTION_DAYS", "0")
    get_settings.cache_clear()

    assert retention.run(now=NOW).visits == 1
    assert kept(visit_id) == []


def test_the_recording_says_it_was_deleted(client: TestClient, org: Org_) -> None:
    visit_id = recorded(org)
    retention.run(now=NOW)
    sign_in(client, org.rep_id)

    audio = client.get(f"/api/visits/{visit_id}/audio")
    shown = client.get(f"/api/visits/{visit_id}").json()

    assert (audio.status_code, audio.json()["error"]) == (
        404,
        {"code": "audio_deleted", "message": "Audio deleted under the retention policy"},
    )
    assert shown["audio_deleted_at"] is not None


# --- old jobs --------------------------------------------------------------------------------


def a_job(state: str, days_ago: int, visit_id: str | None = None) -> str:
    with session_scope() as session:
        job = Job(
            kind=JobKind.EXTRACT if visit_id else JobKind.PING,
            visit_id=visit_id,
            state=state,
            max_attempts=1,
            last_error="It failed." if state == JobState.DEAD else None,
        )
        session.add(job)
        session.flush()
        when = NOW - timedelta(days=days_ago)
        session.execute(update(Job).where(Job.id == job.id).values(updated_at=when))
        return job.id


def test_done_jobs_go_after_a_week_and_dead_ones_after_a_month(org: Org_) -> None:
    failed = recorded(org, VisitStatus.REVIEW)
    move_to(org, failed, VisitStatus.FAILED, reason="The audio can't be read.")
    stay = {a_job(JobState.DONE, 6), a_job(JobState.DEAD, 29), a_job(JobState.QUEUED, 90)}
    go = {a_job(JobState.DONE, 8), a_job(JobState.DEAD, 31, failed)}

    swept = retention.run(now=NOW)

    with session_scope(read_only=True) as session:
        left = set(session.scalars(select(Job.id)).all())
    assert (swept.done_jobs, swept.dead_jobs) == (1, 1)
    assert left == stay
    assert not go & left
    assert load(failed).failure_reason == "The audio can't be read."


# --- the daily job -------------------------------------------------------------------------


def test_housekeeping_queues_it_once_a_day() -> None:
    with session_scope() as session:
        first = retention.queue_due(session)
    with session_scope() as session:
        again = retention.queue_due(session)
    with session_scope() as session:  # it ran, more than a day ago
        session.execute(
            update(Job).values(state=JobState.DONE, created_at=NOW - timedelta(days=1, minutes=1))
        )
    with session_scope() as session:
        next_day = retention.queue_due(session)

    assert (first, again, next_day) == (True, False, True)


def test_the_worker_queues_and_runs_it(org: Org_) -> None:
    visit_id = recorded(org)
    runner = worker.Worker("test-host:1:aaaaaa")

    assert runner.housekeep()["retention_queued"] == 1
    assert not runner.run_once()  # due in a few minutes, not yet
    with session_scope() as session:
        session.execute(update(Job).values(run_after=NOW - timedelta(minutes=1)))
    assert runner.run_once()

    with session_scope(read_only=True) as session:
        job = session.scalars(select(Job).where(Job.kind == JobKind.RETENTION)).one()
    assert (job.state, kept(visit_id)) == (JobState.DONE, [])


def test_a_recording_deleted_after_the_check_is_the_same_404(
    client: TestClient, org: Org_, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    visit_id = recorded(org, days_ago=0)
    sign_in(client, org.rep_id)
    # Retention deleted it between playback_file's check and the send.
    monkeypatch.setattr(visits, "playback_file", lambda *_: tmp_path / "gone.m4a")

    audio = client.get(f"/api/visits/{visit_id}/audio")

    assert (audio.status_code, audio.json()["error"]["code"]) == (404, "audio_deleted")


def test_a_file_that_won_t_go_leaves_the_rest_to_go(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    visit_id = recorded(org)
    stuck = storage.playback_path(visit_id)
    unlink = Path.unlink

    def refusing(path: Path, missing_ok: bool = False) -> None:
        if path == stuck:
            raise PermissionError("in use")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refusing)

    swept = retention.run(now=NOW)

    assert (swept.files, kept(visit_id)) == (5, ["playback.m4a"])
    assert swept.done_jobs == 0  # and the run went on to the jobs


def test_a_run_whose_claim_is_lost_stops_at_once(org: Org_) -> None:
    first, second = recorded(org), recorded(org)
    lost = threading.Event()
    lost.set()  # another worker has the job now

    swept = retention.run(claim_lost=lost, now=NOW)

    assert (swept.visits, swept.files) == (0, 0)
    assert (load(first).audio_deleted_at, len(kept(second))) == (None, 6)
