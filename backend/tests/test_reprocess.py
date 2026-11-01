"""reprocess.py, its API route and scripts/reprocess_failed.py (batch 13)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from countertalk import jobs, reprocess, storage, visits, worker
from countertalk.db import session_scope
from countertalk.errors import Conflict, PermanentError, RetryableError
from countertalk.llm.base import Outcome
from countertalk.models import (
    Job,
    JobKind,
    JobState,
    Shop,
    Transcript,
    Visit,
    VisitClient,
    VisitStatus,
)
from countertalk.playbooks import load_org_pack
from countertalk.users import demo_rep
from scripts import reprocess_failed
from scripts.seed_dev import DEMO_PACK, seed
from tests.conftest import give_transcript, lock_waiters
from tests.test_extraction import PACKS, answer, run, transcribed
from tests.test_roles import ALL_PLACED, FakeLlm, call, tiny_org
from tests.test_transcription import FakeStt, short_chunks, uploaded_visit  # noqa: F401
from tests.test_visits import Org_, load, move_to, new_visit
from tests.test_worker import wait_for

S = VisitStatus


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def work(stt: FakeStt | None = None, llm: FakeLlm | None = None) -> None:
    """One job, the worker's way, with these fakes for its handlers."""
    handlers = {
        JobKind.TRANSCRIBE: worker.transcribe_with(stt or FakeStt()),
        JobKind.EXTRACT: worker.extract_with(llm or FakeLlm(), **PACKS),
    }
    assert worker.Worker("test-host:1:aaaaaa", handlers=handlers).run_once()


def again(org: Org_, visit_id: str, start: reprocess.From) -> Visit:
    with session_scope() as session:
        return reprocess.reprocess(session, org.org_id, visit_id, start)


def queued(visit_id: str) -> list[str]:
    with session_scope(read_only=True) as session:
        query = select(Job.kind).where(Job.visit_id == visit_id, Job.state == JobState.QUEUED)
        return list(session.scalars(query))


def reviewed(org: Org_) -> str:
    """A visit in review: transcribed and extracted."""
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, answer())
    return visit_id


def heard(org: Org_, stt: FakeStt) -> str:
    """An uploaded recording of three 10 s chunks, transcribed by stt: its visit, transcribed,
    with its extract job set aside."""
    visit_id = uploaded_visit(org, 25)
    work(stt)
    with session_scope() as session:
        session.execute(update(Job).where(Job.kind == JobKind.EXTRACT).values(state="done"))
    return visit_id


# --- from extract ----------------------------------------------------------------------------


def test_from_extract_reuses_the_transcript_and_never_calls_speech_to_text(org: Org_) -> None:
    visit_id = reviewed(org)
    stt = FakeStt()

    assert again(org, visit_id, "extract").status == S.TRANSCRIBED
    assert queued(visit_id) == [JobKind.EXTRACT]
    work(stt, FakeLlm(answer()))  # roles are saved: extraction only

    assert stt.calls == []  # §9.4
    assert load(visit_id).status == S.REVIEW


# --- from transcribe -------------------------------------------------------------------------


def test_from_transcribe_makes_a_new_transcript_and_pays_for_every_chunk_again(
    org: Org_,
) -> None:
    visit_id = heard(org, FakeStt())
    move_to(org, visit_id, S.EXTRACTING, S.REVIEW)
    first = load(visit_id).transcript_id
    stt = FakeStt()

    assert again(org, visit_id, "transcribe").status == S.UPLOADED
    assert queued(visit_id) == [JobKind.TRANSCRIBE]
    work(stt)

    visit = load(visit_id)
    assert len(stt.calls) == 3  # the replies saved the first time were removed
    assert (visit.status, visit.transcript_id != first) == (S.TRANSCRIBED, True)
    with session_scope(read_only=True) as session:
        assert len(session.scalars(select(Transcript.id)).all()) == 2


def test_a_visit_that_failed_part_way_through_keeps_the_replies_already_paid_for(
    org: Org_,
) -> None:
    visit_id = uploaded_visit(org, 25)
    work(FakeStt({1: PermanentError("stt_auth_failed", "Speech-to-text refused the key")}))
    assert load(visit_id).status == S.FAILED
    stt = FakeStt()

    again(org, visit_id, "transcribe")
    work(stt)

    assert len(stt.calls) == 2  # chunk 0's reply was saved: chunks 1 and 2 only (R8)
    assert load(visit_id).status == S.TRANSCRIBED
    assert storage.stt_reply_path(visit_id, 0).exists()


def test_a_dead_transcribe_job_after_reprocessing_fails_the_visit_with_a_reason(
    org: Org_,
) -> None:
    visit_id = heard(org, FakeStt())
    move_to(org, visit_id, S.EXTRACTING, S.REVIEW)

    again(org, visit_id, "transcribe")
    work(FakeStt({0: PermanentError("stt_auth_failed", "Speech-to-text refused the key")}))

    visit = load(visit_id)
    assert (visit.status, visit.failure_reason) == (S.FAILED, "Speech-to-text refused the key")


def test_a_dead_job_after_reprocessing_fails_the_visit_with_a_reason(org: Org_) -> None:
    visit_id = reviewed(org)
    invalid = Outcome(
        calls=(call("llm_answer_invalid"), call("llm_answer_invalid")),
        error=RetryableError("llm_answer_invalid", "x"),
    )

    again(org, visit_id, "extract")
    work(llm=FakeLlm(invalid))

    visit = load(visit_id)
    assert (visit.status, visit.failure_reason) == (
        S.FAILED,
        "Extraction invalid: Muse Spark's answer didn't fit, even after a repair",
    )
    with session_scope(read_only=True) as session:
        codes = session.scalars(select(Job.last_error_code).where(Job.state == JobState.DEAD))
        assert codes.all() == ["extraction_invalid"]  # stored, for reprocess_failed


# --- refused ---------------------------------------------------------------------------------


def code_of(org: Org_, visit_id: str, start: reprocess.From) -> str:
    with pytest.raises(Conflict) as refused:
        again(org, visit_id, start)
    return refused.value.code


def test_what_cant_be_reprocessed_is_refused_and_left_as_it_was(org: Org_) -> None:
    confirmed = reviewed(org)
    move_to(org, confirmed, S.CONFIRMED)
    processing = transcribed(org)
    never_uploaded = new_visit(org, client=VisitClient.UPLOAD)
    move_to(org, never_uploaded, S.FAILED, reason="Stopped")
    untranscribed = failed_with(org, "stt_auth_failed", LATER, died=JobKind.TRANSCRIBE)

    assert code_of(org, confirmed, "extract") == "visit_confirmed"
    assert code_of(org, confirmed, "transcribe") == "visit_confirmed"
    assert code_of(org, processing, "extract") == "visit_processing"
    assert code_of(org, never_uploaded, "transcribe") == "upload_not_finished"
    assert code_of(org, untranscribed, "extract") == "no_transcript"
    statuses = [load(v).status for v in (confirmed, processing, never_uploaded, untranscribed)]
    assert statuses == [S.CONFIRMED, S.TRANSCRIBED, S.FAILED, S.FAILED]


def test_a_visit_with_a_job_already_is_refused_and_not_moved(org: Org_) -> None:
    visit_id = uploaded_visit(org, 5)
    move_to(org, visit_id, S.FAILED, reason="Stopped")
    with session_scope() as session:  # its transcribe job, queued before it was failed
        assert session.scalar(select(Job.id).where(Job.visit_id == visit_id)) is not None

    assert code_of(org, visit_id, "transcribe") == "visit_processing"
    assert load(visit_id).status == S.FAILED


def test_a_visit_with_a_job_of_the_other_kind_is_refused_too(org: Org_) -> None:
    visit_id = reviewed(org)
    move_to(org, visit_id, S.FAILED, reason="Stopped")
    with session_scope() as session:
        jobs.enqueue(session, JobKind.TRANSCRIBE, visit_id=visit_id)

    assert code_of(org, visit_id, "extract") == "visit_processing"
    assert (load(visit_id).status, queued(visit_id)) == (S.FAILED, [JobKind.TRANSCRIBE])


def test_a_redo_that_failed_part_way_keeps_what_it_paid_for_when_redone_again(
    org: Org_,
) -> None:
    visit_id = heard(org, FakeStt())
    move_to(org, visit_id, S.EXTRACTING, S.REVIEW)
    again(org, visit_id, "transcribe")
    redo = FakeStt({2: PermanentError("stt_auth_failed", "Speech-to-text refused the key")})
    work(redo)  # chunks 0 and 1 paid for again, then the key is refused
    assert (len(redo.calls), load(visit_id).status) == (3, S.FAILED)
    finish = FakeStt()

    again(org, visit_id, "transcribe")
    work(finish)

    assert len(finish.calls) == 1  # only chunk 2: the redo's own replies were kept (R8)
    assert load(visit_id).status == S.TRANSCRIBED


@pytest.mark.parametrize("start", reprocess.STEPS)
def test_reprocess_and_confirm_cant_interleave(org: Org_, start: reprocess.From) -> None:
    visit_id = heard(org, FakeStt())  # its upload finished: either step could run
    move_to(org, visit_id, S.EXTRACTING, S.REVIEW)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        visits.set_status(visits.lock_visit(session, org.org_id, visit_id), S.CONFIRMED)
        reprocessing = pool.submit(code_of, org, visit_id, start)
        wait_for(lambda: lock_waiters() == 1, "the reprocess never waited for the visit's lock")
        session.commit()

        assert reprocessing.result(timeout=10) == "visit_confirmed"
    assert load(visit_id).status == S.CONFIRMED


# --- the API ---------------------------------------------------------------------------------


@pytest.fixture
def demo_visit() -> str:
    """A visit of the demo org, the API's (batch 16 brings sign-in), in review."""
    with session_scope() as session:
        seed(session, load_org_pack(*DEMO_PACK))
        rep = demo_rep(session)
        shops = select(Shop.id).where(Shop.org_id == rep.org_id).order_by(Shop.name)
        demo = Org_(rep.org_id, rep.id, session.scalars(shops).first())
    visit_id = transcribed(demo)
    move_to(demo, visit_id, S.EXTRACTING, S.REVIEW)
    return visit_id


def test_the_api_reprocesses_from_extract(client: TestClient, demo_visit: str) -> None:
    response = client.post(f"/api/visits/{demo_visit}/reprocess", json={"from": "extract"})

    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["processing"]) == ("transcribed", True)


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"from": "transcribe"}, 409, "upload_not_finished"),  # given its transcript by hand
        ({"from": "the start"}, 422, "invalid_request"),
        ({"from_": "extract"}, 422, "invalid_request"),
        ({}, 422, "invalid_request"),
    ],
)
def test_the_api_s_refusals(
    client: TestClient, demo_visit: str, body: dict, status: int, code: str
) -> None:
    response = client.post(f"/api/visits/{demo_visit}/reprocess", json=body)

    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert load(demo_visit).status == S.REVIEW


def test_another_orgs_visit_is_404(client: TestClient, org: Org_, demo_visit: str) -> None:
    theirs = reviewed(org)

    response = client.post(f"/api/visits/{theirs}/reprocess", json={"from": "extract"})

    assert (response.status_code, response.json()["error"]["code"]) == (404, "visit_not_found")


# --- reprocess_failed.py ---------------------------------------------------------------------

LATER = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def failed_with(org: Org_, code: str | None, at: datetime, *, died: str = JobKind.EXTRACT) -> str:
    """A failed visit whose newest job, of this kind, died with this code at this time. One
    whose extract job died was transcribed; one whose transcribe job died wasn't."""
    visit_id = uploaded_visit(org, 5)  # its transcribe job queued
    with session_scope() as session:
        if died == JobKind.EXTRACT:
            session.execute(update(Job).where(Job.visit_id == visit_id).values(state="done"))
            jobs.enqueue(session, JobKind.EXTRACT, visit_id=visit_id)
        newest = select(Job.id).where(Job.visit_id == visit_id).order_by(Job.seq.desc()).limit(1)
        session.execute(
            update(Job)
            .where(Job.id == newest.scalar_subquery())
            .values(state=JobState.DEAD, last_error_code=code, updated_at=at)
        )
    if died == JobKind.EXTRACT:
        give_transcript(visit_id)
    move_to(org, visit_id, S.FAILED, reason="Stopped")
    return visit_id


def test_only_the_code_and_time_given_are_reprocessed(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    hit = failed_with(org, "llm_auth_failed", LATER)
    hit_mid_stt = failed_with(org, "llm_auth_failed", LATER, died=JobKind.TRANSCRIBE)
    other_code = failed_with(org, "llm_unavailable", LATER)
    too_early = failed_with(org, "llm_auth_failed", LATER - timedelta(days=2))
    uncoded = failed_with(org, None, LATER)
    args = ["--code", "llm_auth_failed", "--since", "2026-11-02"]

    assert reprocess_failed.main(args) == 0  # the dry run changes nothing

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "2 failed visits with llm_auth_failed since 2026-11-02T00:00:00+05:30"
    assert sorted(line.split()[0] for line in out[1:3]) == sorted([hit, hit_mid_stt])
    assert out[3:] == [
        "  not matched: 1 failed before batch 13, with no code stored",
        "from transcribe: speech-to-text is paid for (saved replies reused)",
        "--yes reprocesses them",
    ]
    assert all(load(v).status == S.FAILED for v in (hit, hit_mid_stt))

    assert reprocess_failed.main([*args, "--yes"]) == 0

    statuses = [load(v).status for v in (hit, hit_mid_stt, other_code, too_early, uncoded)]
    assert statuses == [S.TRANSCRIBED, S.UPLOADED, S.FAILED, S.FAILED, S.FAILED]  # §9.7


def test_the_step_rerun_is_the_one_whose_job_died(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    # A redo from transcribe died, so the visit still has its old transcript: it isn't
    # re-extracted from that, it's transcribed again.
    visit_id = heard(org, FakeStt())
    move_to(org, visit_id, S.EXTRACTING, S.REVIEW)
    again(org, visit_id, "transcribe")
    work(FakeStt({0: PermanentError("stt_auth_failed", "Speech-to-text refused the key")}))

    reprocess_failed.main(["--code", "stt_auth_failed", "--since", "2026-11-01"])

    assert capsys.readouterr().out.splitlines()[1].endswith(", from transcribe")


@pytest.mark.parametrize("since", ["2026-11-02T12:00:00", "yesterday", "2026-13-01"])
def test_since_needs_a_date_or_a_time_with_its_zone(since: str) -> None:
    with pytest.raises(SystemExit):
        reprocess_failed.main(["--code", "x", "--since", since])


def test_a_visit_changed_meanwhile_is_one_line_and_the_rest_go_on(
    org: Org_, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = failed_with(org, "llm_auth_failed", LATER)
    second = failed_with(org, "llm_auth_failed", LATER + timedelta(minutes=1))
    real = reprocess.reprocess

    def confirmed_meanwhile(session, org_id, visit_id, start):  # type: ignore[no-untyped-def]
        if visit_id == first:
            raise Conflict("visit_confirmed", "A confirmed visit can't be processed again")
        return real(session, org_id, visit_id, start)

    monkeypatch.setattr(reprocess_failed, "reprocess", confirmed_meanwhile)

    assert (
        reprocess_failed.main(["--code", "llm_auth_failed", "--since", "2026-11-02", "--yes"]) == 1
    )

    assert capsys.readouterr().err == (
        f"  {first}: not reprocessed: visit_confirmed: A confirmed visit can't be processed again\n"
    )
    assert load(second).status == S.TRANSCRIBED


def test_a_stale_claim_stores_its_code(org: Org_) -> None:
    visit_id = transcribed(org)
    with session_scope() as session:
        jobs.enqueue(session, JobKind.EXTRACT, visit_id=visit_id)
        jobs.claim_for_visit(session, "test-host:1:aaaaaa", visit_id, JobKind.EXTRACT)
        session.execute(update(Job).values(locked_at=datetime(2026, 1, 1, tzinfo=UTC)))

    jobs.recover_stale(visits.fail_processing)

    with session_scope(read_only=True) as session:
        assert session.scalars(select(Job.last_error_code)).all() == [jobs.STALE_CODE]
