"""jobs.py: the job queue in Postgres (BRIEF §7; CODING_GUIDELINES §2.3, §9.1, §9.2, §9.5)."""

import logging
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from countertalk import jobs, visits
from countertalk.db import session_scope
from countertalk.errors import Conflict, PermanentError, RetryableError
from countertalk.limits import (
    JOB_BACKOFF_BASE_MS,
    JOB_BACKOFF_JITTER,
    JOB_BACKOFF_MAX_MS,
    JOB_HEARTBEAT_MS,
    JOB_MAX_ATTEMPTS,
    LOCK_TIMEOUT_MS,
    STALE_JOB_MS,
)
from countertalk.models import Job, JobKind, JobState, VisitStatus
from tests.test_visits import BLOCKED_S, RACE_TIMEOUT_S, Org_, load, make_org, visit_at

WORKER = "test-host:1:aaaaaa"
OTHER_WORKER = "test-host:2:bbbbbb"
# How far a time read in one transaction may be from one read in the next.
CLOCK_SLACK = timedelta(seconds=2)


@pytest.fixture
def org() -> Org_:
    return make_org()


def queue(kind: str = JobKind.PING, visit_id: str | None = None) -> str:
    with session_scope() as session:
        job_id = jobs.enqueue(session, kind, visit_id=visit_id)
    assert job_id is not None
    return job_id


def claim(worker: str = WORKER) -> jobs.ClaimedJob | None:
    with session_scope() as session:
        return jobs.claim(session, worker)


def claimed(worker: str = WORKER) -> jobs.ClaimedJob:
    job = claim(worker)
    assert job is not None
    return job


def running(kind: str = JobKind.PING) -> jobs.ClaimedJob:
    """A job of no visit, queued and claimed by WORKER."""
    queue(kind)
    return claimed()


def fail(job: jobs.ClaimedJob, error: BaseException, worker: str = WORKER) -> str | None:
    with session_scope() as session:
        return jobs.mark_failed(session, job, worker, error)


def recover() -> list[jobs.Recovered]:
    return jobs.recover_stale()


def job_row(job_id: str) -> Job:
    with session_scope(read_only=True) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job


def all_job_ids() -> list[str]:
    with session_scope(read_only=True) as session:
        return list(session.scalars(select(Job.id).order_by(Job.seq)))


def db_now() -> datetime:
    with session_scope(read_only=True) as session:
        now = session.scalar(select(func.now()))
        assert now is not None
        return now


def set_times(job_id: str, **ago_ms: int) -> None:
    """Moves a job's times into the past, by Postgres's clock: set_times(id, locked_at=1000)."""
    values = {column: func.now() - timedelta(milliseconds=ms) for column, ms in ago_ms.items()}
    with session_scope() as session:
        session.execute(update(Job).where(Job.id == job_id).values(**values))


def make_stale(job_id: str) -> None:
    set_times(job_id, locked_at=STALE_JOB_MS + 1_000)


def make_due(job_id: str) -> None:
    set_times(job_id, run_after=1_000)


def lose(job: jobs.ClaimedJob) -> None:
    """The claim goes stale, is recovered, and another worker takes the job."""
    make_stale(job.id)
    assert [r.job.id for r in recover()] == [job.id]
    assert claimed(OTHER_WORKER).id == job.id


def transcribe_job(org: Org_) -> tuple[str, jobs.ClaimedJob]:
    """A visit being transcribed, and its claimed transcribe job."""
    visit_id = visit_at(org, VisitStatus.TRANSCRIBING)
    queue(JobKind.TRANSCRIBE, visit_id)
    return visit_id, claimed()


# --- enqueue ----------------------------------------------------------------------------------


def test_a_queued_job_is_due_now_with_no_attempts() -> None:
    job = job_row(queue())

    assert (job.state, job.attempts, job.max_attempts) == (JobState.QUEUED, 0, JOB_MAX_ATTEMPTS)
    assert job.visit_id is None
    assert abs(job.run_after - db_now()) < CLOCK_SLACK


def test_only_one_active_job_per_visit_and_kind(org: Org_) -> None:
    visit_id = visit_at(org, VisitStatus.UPLOADED)
    first = queue(JobKind.TRANSCRIBE, visit_id)

    with session_scope() as session:
        again = jobs.enqueue(session, JobKind.TRANSCRIBE, visit_id=visit_id)

    assert again is None
    assert all_job_ids() == [first]


def test_a_running_job_is_active_too(org: Org_) -> None:
    visit_id, _ = transcribe_job(org)

    with session_scope() as session:
        assert jobs.enqueue(session, JobKind.TRANSCRIBE, visit_id=visit_id) is None


def test_another_kind_for_the_same_visit_is_queued(org: Org_) -> None:
    visit_id = visit_at(org, VisitStatus.UPLOADED)
    queue(JobKind.TRANSCRIBE, visit_id)

    queue(JobKind.EXTRACT, visit_id)

    assert len(all_job_ids()) == 2


@pytest.mark.parametrize("error", [None, PermanentError("bad_input", "It can't be read.")])
def test_a_finished_job_doesnt_block_a_new_one(org: Org_, error: BaseException | None) -> None:
    visit_id = visit_at(org, VisitStatus.UPLOADED)
    first = queue(JobKind.TRANSCRIBE, visit_id)
    job = claimed()
    with session_scope() as session:
        if error is None:
            jobs.mark_done(session, job, WORKER)
        else:
            jobs.mark_failed(session, job, WORKER, error)

    second = queue(JobKind.TRANSCRIBE, visit_id)

    assert all_job_ids() == [first, second]


def test_jobs_without_a_visit_are_never_merged() -> None:
    for _ in range(3):
        queue(JobKind.PING)

    assert len(all_job_ids()) == 3


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError), session_scope() as session:
        jobs.enqueue(session, "dance")


def test_every_kind_has_its_max_attempts() -> None:
    assert set(jobs.KINDS) == set(JobKind)


def test_a_kind_can_set_its_own_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(jobs.KINDS, JobKind.SLEEP, 2)

    assert job_row(queue(JobKind.SLEEP)).max_attempts == 2


# --- claim ------------------------------------------------------------------------------------


def test_a_claim_takes_the_oldest_due_job_and_marks_it_running() -> None:
    first = queue()
    queue()

    job = claimed()

    assert (job.id, job.kind, job.attempts) == (first, JobKind.PING, 1)
    row = job_row(first)
    assert (row.state, row.locked_by) == (JobState.RUNNING, WORKER)
    assert row.locked_at is not None
    assert abs(row.locked_at - db_now()) < CLOCK_SLACK


def test_jobs_queued_in_the_same_tick_are_claimed_in_seq_order() -> None:
    with session_scope() as session:
        # One transaction, so Postgres's now() is the same for all of them.
        ids = [jobs.enqueue(session, JobKind.PING) for _ in range(5)]
    assert len({job_row(job_id).created_at for job_id in ids if job_id}) == 1

    assert [claimed().id for _ in ids] == ids


def test_the_oldest_job_is_claimed_first_wherever_its_row_is_stored() -> None:
    queue()
    older = queue()
    # An UPDATE writes a new row version at the end of the table, so the older job is stored
    # last, and a scan without ORDER BY would find the other one first.
    set_times(older, created_at=60_000)

    assert claimed().id == older


def test_with_nothing_due_there_is_nothing_to_claim() -> None:
    assert claim() is None
    job_id = queue()
    set_times(job_id, run_after=-60_000)  # a minute from now

    assert claim() is None


def test_a_claim_skips_a_job_another_transaction_has_locked() -> None:
    first = queue()
    second = queue()

    with session_scope() as holder:
        holder.execute(select(Job.id).where(Job.id == first).with_for_update())
        started = time.monotonic()
        job = claimed()
        waited_s = time.monotonic() - started

    assert job.id == second
    assert waited_s < LOCK_TIMEOUT_MS / 1000 / 2


def test_two_workers_claiming_at_once_never_take_the_same_job() -> None:
    ids = [queue() for _ in range(20)]
    start = threading.Barrier(4)
    taken: list[str] = []
    taken_lock = threading.Lock()

    def work(worker: str) -> None:
        start.wait()
        while (job := claim(worker)) is not None:
            with taken_lock:
                taken.append(job.id)

    threads = [threading.Thread(target=work, args=(f"worker-{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(RACE_TIMEOUT_S)

    assert sorted(taken) == sorted(ids)


# --- heartbeat --------------------------------------------------------------------------------


def test_a_heartbeat_refreshes_the_claim() -> None:
    job = running()
    set_times(job.id, locked_at=JOB_HEARTBEAT_MS)

    with session_scope() as session:
        assert jobs.heartbeat(session, job, WORKER) is True

    locked_at = job_row(job.id).locked_at
    assert locked_at is not None
    assert abs(locked_at - db_now()) < CLOCK_SLACK


def test_a_job_whose_heartbeat_runs_is_never_recovered() -> None:
    job = running()
    make_stale(job.id)
    with session_scope() as session:
        jobs.heartbeat(session, job, WORKER)

    assert recover() == []
    assert job_row(job.id).state == JobState.RUNNING


def test_a_heartbeat_for_a_lost_claim_changes_nothing() -> None:
    job = running()
    lose(job)

    with session_scope() as session:
        assert jobs.heartbeat(session, job, WORKER) is False

    assert job_row(job.id).locked_by == OTHER_WORKER


# --- mark_done --------------------------------------------------------------------------------


def test_a_finished_job_is_done_and_released() -> None:
    job = running()

    with session_scope() as session:
        assert jobs.mark_done(session, job, WORKER) is True

    row = job_row(job.id)
    assert (row.state, row.locked_by, row.locked_at) == (JobState.DONE, None, None)


def test_finishing_a_lost_claim_changes_nothing_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    job = running()
    lose(job)

    with caplog.at_level(logging.WARNING), session_scope() as session:
        assert jobs.mark_done(session, job, WORKER) is False

    row = job_row(job.id)
    assert (row.state, row.locked_by) == (JobState.RUNNING, OTHER_WORKER)
    assert f"job={job.id}" in caplog.text
    assert "claim lost" in caplog.text


# --- mark_failed ------------------------------------------------------------------------------


def test_a_retryable_error_is_retried_later_with_backoff() -> None:
    job = running()

    assert fail(job, RetryableError("stt_busy", "The speech service is busy.")) == JobState.QUEUED

    row = job_row(job.id)
    delay = row.run_after - db_now()
    shortest = timedelta(milliseconds=JOB_BACKOFF_BASE_MS * (1 - JOB_BACKOFF_JITTER))
    longest = timedelta(milliseconds=JOB_BACKOFF_BASE_MS * (1 + JOB_BACKOFF_JITTER))
    assert shortest - CLOCK_SLACK <= delay <= longest
    assert (row.state, row.locked_by, row.locked_at) == (JobState.QUEUED, None, None)
    assert row.last_error == "RetryableError: stt_busy: The speech service is busy."
    assert claim() is None


def test_a_permanent_error_kills_the_job_at_once_and_fails_its_visit(org: Org_) -> None:
    visit_id, job = transcribe_job(org)
    assert job.attempts < job.max_attempts

    state = fail(job, PermanentError("unreadable_audio", "The recording can't be read."))

    assert state == JobState.DEAD
    assert job_row(job.id).state == JobState.DEAD
    visit = load(visit_id)
    assert visit.status == VisitStatus.FAILED
    assert visit.failure_reason == "The recording can't be read."


def test_any_other_domain_error_is_permanent_too() -> None:
    job = running()

    assert fail(job, Conflict("invalid_status_change", "It moved on.")) == JobState.DEAD


def test_an_error_that_isnt_ours_is_retried_and_its_text_is_never_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job = running()

    with caplog.at_level(logging.DEBUG):
        state = fail(job, ValueError("Key (email)=(someone@example.com) already exists"))

    assert state == JobState.QUEUED
    assert job_row(job.id).last_error == "ValueError"
    assert "someone" not in caplog.text
    assert "ValueError" in caplog.text


def test_a_job_that_fails_on_its_last_try_dies_and_fails_its_visit(org: Org_) -> None:
    visit_id = visit_at(org, VisitStatus.TRANSCRIBING)
    job_id = queue(JobKind.TRANSCRIBE, visit_id)
    states = []
    for _ in range(JOB_MAX_ATTEMPTS):
        make_due(job_id)
        states.append(fail(claimed(), RetryableError("stt_busy", "The speech service is busy.")))

    assert states == [JobState.QUEUED] * (JOB_MAX_ATTEMPTS - 1) + [JobState.DEAD]
    visit = load(visit_id)
    assert visit.status == VisitStatus.FAILED
    assert visit.failure_reason == jobs.gave_up(JOB_MAX_ATTEMPTS)


def test_a_dying_job_without_a_visit_just_dies() -> None:
    job = running()

    assert fail(job, PermanentError("bad_input", "It can't be read.")) == JobState.DEAD


def test_failing_a_lost_claim_changes_nothing_and_leaves_the_visit_alone(org: Org_) -> None:
    visit_id, job = transcribe_job(org)
    lose(job)

    assert fail(job, PermanentError("unreadable_audio", "The recording can't be read.")) is None

    row = job_row(job.id)
    assert (row.state, row.locked_by) == (JobState.RUNNING, OTHER_WORKER)
    assert load(visit_id).status == VisitStatus.TRANSCRIBING


def test_a_dying_job_locks_its_visit_before_the_job(org: Org_) -> None:
    # The lock order is visit, then job, as in batch 6's complete (lock the visit, then
    # enqueue). The other order deadlocks with it.
    visit_id, job = transcribe_job(org)
    visit_locked = threading.Event()
    results: list[object] = []

    def complete_and_enqueue() -> None:
        try:
            with session_scope() as session:
                visits.lock_visit(session, org.org_id, visit_id)
                visit_locked.set()
                time.sleep(BLOCKED_S)  # meanwhile, the job is being failed
                results.append(jobs.enqueue(session, JobKind.TRANSCRIBE, visit_id=visit_id))
        except SQLAlchemyError as error:
            results.append(error)

    thread = threading.Thread(target=complete_and_enqueue)
    thread.start()
    assert visit_locked.wait(RACE_TIMEOUT_S)
    state = fail(job, PermanentError("unreadable_audio", "The recording can't be read."))
    thread.join(RACE_TIMEOUT_S)

    assert results == [None]  # the running job was still active, so nothing new was queued
    assert state == JobState.DEAD
    assert load(visit_id).status == VisitStatus.FAILED


# --- recover_stale ----------------------------------------------------------------------------


def test_a_stale_job_with_tries_left_is_queued_again_at_once() -> None:
    job = running()
    make_stale(job.id)

    recovered = recover()

    assert [(r.job.id, r.state) for r in recovered] == [(job.id, JobState.QUEUED)]
    row = job_row(job.id)
    assert (row.state, row.locked_by, row.locked_at) == (JobState.QUEUED, None, None)
    assert row.last_error == jobs.STALE_ERROR
    again = claimed(OTHER_WORKER)
    assert (again.id, again.attempts) == (job.id, 2)


def test_a_running_job_that_isnt_stale_is_left_alone() -> None:
    job = running()

    assert recover() == []
    assert job_row(job.id).locked_by == WORKER


def test_a_job_stale_on_its_last_try_dies_and_fails_its_visit(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(jobs.KINDS, JobKind.TRANSCRIBE, 1)
    visit_id, job = transcribe_job(org)
    make_stale(job.id)

    recovered = recover()

    assert [(r.job.id, r.state) for r in recovered] == [(job.id, JobState.DEAD)]
    visit = load(visit_id)
    assert visit.status == VisitStatus.FAILED
    assert visit.failure_reason == jobs.interrupted(1)


def test_a_heartbeat_that_lands_during_recovery_keeps_the_job() -> None:
    job = running()
    make_stale(job.id)
    found: list[list[jobs.Recovered]] = []

    with session_scope() as beating:
        # The heartbeat's transaction has the row: recovery reads the job as stale, then waits.
        beating.execute(select(Job.id).where(Job.id == job.id).with_for_update())
        thread = threading.Thread(target=lambda: found.append(recover()))
        thread.start()
        thread.join(BLOCKED_S)
        assert thread.is_alive()
        assert jobs.heartbeat(beating, job, WORKER) is True
    thread.join(RACE_TIMEOUT_S)

    assert found == [[]]
    assert job_row(job.id).locked_by == WORKER


def test_two_housekeepers_recovering_at_once_recover_each_job_once() -> None:
    for _ in range(5):
        queue()
    ids = [claimed().id for _ in range(5)]
    for job_id in ids:
        make_stale(job_id)
    start = threading.Barrier(2)
    recovered: list[str] = []
    recovered_lock = threading.Lock()

    def housekeep() -> None:
        start.wait()
        found = recover()
        with recovered_lock:
            recovered.extend(r.job.id for r in found)

    threads = [threading.Thread(target=housekeep) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(RACE_TIMEOUT_S)

    assert sorted(recovered) == sorted(ids)


# --- backoff and the stored error ---------------------------------------------------------------


def test_backoff_doubles_each_try_up_to_its_cap() -> None:
    def no_jitter(low: float, high: float) -> float:
        return 1.0

    delays = [jobs.backoff_ms(attempt, uniform=no_jitter) for attempt in range(1, 9)]

    expected = [min(JOB_BACKOFF_BASE_MS * 2 ** (n - 1), JOB_BACKOFF_MAX_MS) for n in range(1, 9)]
    assert delays == expected
    assert delays[-1] == JOB_BACKOFF_MAX_MS


def test_backoff_jitter_stays_within_its_bounds() -> None:
    def lowest(low: float, high: float) -> float:
        return low

    def highest(low: float, high: float) -> float:
        return high

    assert jobs.backoff_ms(1, uniform=lowest) == JOB_BACKOFF_BASE_MS * (1 - JOB_BACKOFF_JITTER)
    assert jobs.backoff_ms(1, uniform=highest) == JOB_BACKOFF_BASE_MS * (1 + JOB_BACKOFF_JITTER)


def test_a_stored_error_keeps_our_own_words_and_only_the_type_of_others() -> None:
    ours = PermanentError("unreadable_audio", "The recording can't be read.")

    assert jobs.error_text(ours) == "PermanentError: unreadable_audio: The recording can't be read."
    assert jobs.error_text(KeyError("someone@example.com")) == "KeyError"


def test_the_stale_limit_is_several_heartbeats_long() -> None:
    # §9.2: a missed heartbeat or two mustn't let another worker take a running job.
    assert STALE_JOB_MS >= 3 * JOB_HEARTBEAT_MS


# --- found by review ----------------------------------------------------------------------------


def test_a_permanent_error_without_words_still_fails_the_visit(org: Org_) -> None:
    visit_id, job = transcribe_job(org)

    assert fail(job, PermanentError("bad_audio", "")) == JobState.DEAD

    visit = load(visit_id)
    assert visit.status == VisitStatus.FAILED
    assert visit.failure_reason == jobs.gave_up(1)


def test_the_reasons_count_their_tries_in_plain_words() -> None:
    assert jobs.gave_up(1).endswith("after 1 try")
    assert jobs.gave_up(5).endswith("after 5 tries")
    assert jobs.interrupted(1).startswith("Processing was interrupted once")
    assert jobs.interrupted(3).startswith("Processing was interrupted 3 times")


def test_an_old_claim_cant_finish_a_newer_claim_by_the_same_worker() -> None:
    job = running()
    make_stale(job.id)
    recover()
    again = claimed()
    assert (again.id, again.attempts) == (job.id, 2)

    with session_scope() as session:
        assert jobs.mark_done(session, job, WORKER) is False

    assert job_row(job.id).state == JobState.RUNNING


def test_recovery_never_holds_one_job_while_it_waits_for_a_visit(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two stale jobs of one visit: the first has tries left, the second is on its last. A request
    # locks the visit and queues a job between them. Holding the first job's change while
    # waiting for that visit would deadlock with the request.
    monkeypatch.setitem(jobs.KINDS, JobKind.EXTRACT, 1)
    visit_id = visit_at(org, VisitStatus.TRANSCRIBING)
    queue(JobKind.TRANSCRIBE, visit_id)
    first = claimed()
    queue(JobKind.EXTRACT, visit_id)
    second = claimed()
    make_stale(first.id)
    make_stale(second.id)
    request_has_visit = threading.Event()
    results: list[object] = []

    def request() -> None:
        try:
            with session_scope() as session:
                visits.lock_visit(session, org.org_id, visit_id)
                request_has_visit.set()
                results.append(jobs.enqueue(session, JobKind.TRANSCRIBE, visit_id=visit_id))
        except SQLAlchemyError as error:
            results.append(error)

    thread = threading.Thread(target=request)
    real_fail_processing = visits.fail_processing

    def fail_once_the_request_has_the_visit(session: Session, *args: str) -> bool:
        thread.start()
        assert request_has_visit.wait(RACE_TIMEOUT_S)
        time.sleep(BLOCKED_S)  # the request is queuing its job
        return real_fail_processing(session, *args)

    monkeypatch.setattr(visits, "fail_processing", fail_once_the_request_has_the_visit)

    recovered = recover()
    thread.join(RACE_TIMEOUT_S)

    assert results == [None]  # the first job was queued again, so it's still the active one
    assert [(r.job.id, r.state) for r in recovered] == [
        (first.id, JobState.QUEUED),
        (second.id, JobState.DEAD),
    ]


def test_a_job_that_cant_be_recovered_doesnt_undo_the_others(
    org: Org_, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(jobs.KINDS, JobKind.TRANSCRIBE, 1)
    visit_id, stuck = transcribe_job(org)
    other = running()
    make_stale(stuck.id)
    make_stale(other.id)

    def lock_times_out(session: Session, *args: str) -> bool:
        raise OperationalError("SELECT ... FOR UPDATE", None, Exception("lock timeout"))

    monkeypatch.setattr(visits, "fail_processing", lock_times_out)

    with caplog.at_level(logging.WARNING):
        recovered = recover()

    assert [r.job.id for r in recovered] == [other.id]
    assert job_row(stuck.id).state == JobState.RUNNING  # the next housekeeping tries again
    assert job_row(other.id).state == JobState.QUEUED
    assert f"job={stuck.id}" in caplog.text
