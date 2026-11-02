"""The job queue in Postgres (BRIEF §7; CODING_GUIDELINES §2.3, §9.1, §9.2, §9.5).

- Job times are Postgres's now(), never Python's clock, so workers on different machines agree
  on what is due and what is stale.
- Each function is one step of the worker's, in its own short transaction. The handler runs
  with none open (§2.4).
- heartbeat, mark_done and mark_failed change a job only while this claim of it is still held.
  If the claim was lost (recovered as stale, and perhaps taken again), they change nothing, so
  a newer claim is never overwritten.
- Lock order: a visit, then its job, and never one job while waiting for another's visit. An API
  request locks its visit, then queues a job, and any other order deadlocks with it. A summary
  (batch 15b) is like a visit: the summary, then its job.
"""

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import ColumnElement, ScalarSelect, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# limits as a module, so a test or a manual run can shorten the stale limit or the backoff for
# everything here. Max attempts are per kind, in KINDS.
from countertalk import limits
from countertalk.db import session_scope
from countertalk.errors import DomainError, RetryableError, describe
from countertalk.models import ACTIVE_JOB, Job, JobKind, JobState, Summary, SummaryStatus, new_id

log = logging.getLogger(__name__)

# What fails a dead job's visit, with a reason the rep can read: visits.fail_processing, passed in
# by the worker. Required, so no caller can leave a visit stuck. jobs.py imports no domain module:
# from batch 6, visits.complete queues a job, so that would be a cycle.
FailVisit = Callable[[Session, str, str], object]

# Each kind's max attempts, fixed into a job when it's queued, so a change applies to new jobs.
KINDS: dict[JobKind, int] = {
    JobKind.TRANSCRIBE: limits.JOB_MAX_ATTEMPTS,
    JobKind.EXTRACT: limits.JOB_MAX_ATTEMPTS,
    JobKind.SUMMARY: limits.JOB_MAX_ATTEMPTS,
    JobKind.RETENTION: limits.JOB_MAX_ATTEMPTS,
    JobKind.PING: limits.JOB_MAX_ATTEMPTS,
    JobKind.SLEEP: limits.JOB_MAX_ATTEMPTS,
}


def gave_up(tries: int) -> str:
    """What the rep reads when a visit's job kept failing (§9.1)."""
    return (
        f"Processing kept failing, so it stopped after {tries} {'try' if tries == 1 else 'tries'}"
    )


def interrupted(tries: int) -> str:
    """What the rep reads when a visit's job kept losing its worker (§9.1)."""
    times = "once" if tries == 1 else f"{tries} times"
    return f"Processing was interrupted {times}, so it stopped"


# last_error for a claim nobody refreshed: there's no exception, the worker just went quiet.
STALE_CODE = "stale_claim"
STALE_ERROR = f"{STALE_CODE}: the worker running it stopped refreshing its claim"


@dataclass(frozen=True)
class ClaimedJob:
    """A job as this worker claimed it: what its handler gets."""

    id: str
    kind: str
    visit_id: str | None
    # Claims so far, this one included.
    attempts: int
    max_attempts: int
    # A summary job's summary (batch 15b).
    summary_id: str | None = None


@dataclass(frozen=True)
class Recovered:
    job: ClaimedJob
    # Queued again, or dead.
    state: str


class _ClaimLost(Exception):
    """Rolls back the savepoint of a step whose job had moved on."""


def enqueue(
    session: Session, kind: str, *, visit_id: str | None = None, summary_id: str | None = None
) -> str | None:
    """Queues a job, due now: its ID, or None if the visit, or the summary, has an active one
    of this kind. The test jobs, with neither, are never merged.
    """
    max_attempts = KINDS[JobKind(kind)]  # ValueError for a kind that doesn't exist
    table = Job.__table__
    row = session.execute(
        insert(table)
        .values(
            id=new_id(),
            visit_id=visit_id,
            summary_id=summary_id,
            kind=kind,
            state=JobState.QUEUED,
            attempts=0,
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(
            index_elements=["summary_id" if summary_id else "visit_id", "kind"],
            index_where=text(ACTIVE_JOB),
        )
        .returning(table.c.id)
    ).first()
    about = f"visit={visit_id}" + (f" summary={summary_id}" if summary_id else "")
    if row is None:
        log.info("job already active kind=%s %s", kind, about)
        return None
    log.info("job queued job=%s kind=%s %s attempt=0", row.id, kind, about)
    return row.id


def claim(session: Session, worker_id: str) -> ClaimedJob | None:
    """Takes the oldest due job, or None if nothing is due.

    One statement, so two workers never take the same job; SKIP LOCKED, so they never wait for
    each other (§2.3).
    """
    due = (
        select(Job.id)
        .where(Job.state == JobState.QUEUED, Job.run_after <= func.now())
        .order_by(Job.created_at, Job.seq)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return _take(session, due, worker_id)


def claim_for_visit(
    session: Session, worker_id: str, visit_id: str, kind: str
) -> ClaimedJob | None:
    """Takes the visit's queued job of this kind, due or not, for a run by hand
    (scripts/transcribe_visit.py). None if there's none queued: running, say, so no second
    copy and no worker can run it at the same time."""
    queued = (
        select(Job.id)
        .where(Job.visit_id == visit_id, Job.kind == kind, Job.state == JobState.QUEUED)
        .order_by(Job.seq)
        .limit(1)  # the partial unique index allows one anyway
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return _take(session, queued, worker_id)


def claim_for_summary(session: Session, worker_id: str, summary_id: str) -> ClaimedJob | None:
    """Takes the summary's queued job, due or not, for a run by hand (scripts/write_summary.py),
    as claim_for_visit does a visit's."""
    queued = (
        select(Job.id)
        .where(Job.summary_id == summary_id, Job.state == JobState.QUEUED)
        .order_by(Job.seq)
        .limit(1)  # the partial unique index allows one anyway
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    return _take(session, queued, worker_id)


def _take(session: Session, job_id: ScalarSelect[Any], worker_id: str) -> ClaimedJob | None:
    row = session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            state=JobState.RUNNING,
            locked_by=worker_id,
            locked_at=func.now(),
            attempts=Job.attempts + 1,
            updated_at=func.now(),
        )
        .returning(Job.id, Job.kind, Job.visit_id, Job.attempts, Job.max_attempts, Job.summary_id)
        .execution_options(synchronize_session=False)
    ).first()
    return None if row is None else ClaimedJob(*row)


def heartbeat(session: Session, job: ClaimedJob, worker_id: str) -> bool:
    """Refreshes this worker's claim (§9.2). False if it was lost; then nothing changed."""
    return _change_held(session, job, worker_id, "heartbeat", {"locked_at": func.now()})


def mark_done(session: Session, job: ClaimedJob, worker_id: str) -> bool:
    """False if the claim was lost: the result is dropped, and the job is left to its new owner."""
    done = _change_held(session, job, worker_id, "mark_done", _released(JobState.DONE))
    if done:
        log.info("job done %s", log_fields(job))
    return done


def mark_failed(
    session: Session,
    job: ClaimedJob,
    worker_id: str,
    error: BaseException,
    fail_visit: FailVisit,
) -> str | None:
    """Queues the job again after a backoff, or marks it dead and fails its visit (§9.1, §9.5).

    Dead on a permanent error, or out of attempts. Returns the new state, or None if the claim
    was lost; then nothing changed, the visit included.
    """
    permanent = is_permanent(error)
    dead = permanent or job.attempts >= job.max_attempts
    values = _released(
        JobState.DEAD if dead else JobState.QUEUED,
        last_error=error_text(error),
        last_error_code=error_code(error),
    )
    if not dead:
        values["run_after"] = func.now() + timedelta(milliseconds=backoff_ms(job.attempts))
    reason = None
    if dead:
        # A permanent error of ours says what's wrong in words the rep can read, if it has any.
        words = error.message if permanent and isinstance(error, DomainError) else ""
        reason = words or gave_up(job.attempts)
    if not _finish(session, job, _held_by(job, worker_id), values, reason, fail_visit):
        _log_lost(job, worker_id, "mark_failed")
        return None
    level, outcome = (logging.ERROR, "dead") if dead else (logging.WARNING, "retrying")
    log.log(level, "job %s %s error=%s", outcome, log_fields(job), values["last_error"])
    return values["state"]


def recover_stale(fail_visit: FailVisit) -> list[Recovered]:
    """Releases jobs whose worker stopped refreshing its claim (§9.1, §9.2).

    One with attempts left is queued again, due at once. One on its last try dies, and fails its
    visit. Each job is its own transaction: one that fails (its visit's lock timing out) doesn't
    undo the rest, and no job is held while another's visit is waited for. Safe while another
    worker does the same: a job is changed only if it's still the stale claim that was read.
    """
    stale_before = func.now() - timedelta(milliseconds=limits.STALE_JOB_MS)
    with session_scope(read_only=True) as session:
        rows = session.execute(
            select(
                Job.id,
                Job.kind,
                Job.visit_id,
                Job.attempts,
                Job.max_attempts,
                Job.locked_by,
                Job.summary_id,
            )
            .where(Job.state == JobState.RUNNING, Job.locked_at < stale_before)
            .order_by(Job.seq)
        ).all()
    recovered = []
    for job_id, kind, visit_id, attempts, max_attempts, locked_by, summary_id in rows:
        job = ClaimedJob(job_id, kind, visit_id, attempts, max_attempts, summary_id)
        dead = attempts >= max_attempts
        values = _released(
            JobState.DEAD if dead else JobState.QUEUED,
            last_error=STALE_ERROR,
            last_error_code=STALE_CODE,
        )
        if not dead:
            values["run_after"] = func.now()
        still_stale = (*_held_by(job, locked_by), Job.locked_at < stale_before)
        try:
            with session_scope() as session:
                reason = interrupted(attempts) if dead else None
                found = _finish(session, job, still_stale, values, reason, fail_visit)
        except (SQLAlchemyError, DomainError) as error:
            # Still running and stale, so the next housekeeping tries it again.
            log.warning("stale job not recovered %s error=%s", log_fields(job), error_text(error))
            continue
        if found:
            log.log(
                logging.ERROR if dead else logging.WARNING,
                "stale job %s %s locked_by=%s",
                "dead" if dead else "queued again",
                log_fields(job),
                locked_by,
            )
            recovered.append(Recovered(job, values["state"]))
    return recovered


def is_permanent(error: BaseException) -> bool:
    """Every error of ours but RetryableError is one retrying can't change (§9.5).

    A foreign exception is retried: a lock timeout or a dropped connection is the common case,
    and a real bug just dies after its attempts.
    """
    return isinstance(error, DomainError) and not isinstance(error, RetryableError)


def error_code(error: BaseException) -> str:
    """The code a failure is matched on: our own, or unexpected_error for anyone else's."""
    return error.code if isinstance(error, DomainError) else "unexpected_error"


def error_text(error: BaseException) -> str:
    """For last_error and logs: our own words, or only the type of anyone else's (§1.9, §2.8)."""
    if isinstance(error, DomainError):
        return f"{type(error).__name__}: {describe(error)}"
    return type(error).__name__


def backoff_ms(attempt: int, uniform: Callable[[float, float], float] = random.uniform) -> int:
    """The wait after a failed try: doubling from the base, capped, then jittered."""
    delay = min(limits.JOB_BACKOFF_BASE_MS * 2 ** (attempt - 1), limits.JOB_BACKOFF_MAX_MS)
    return round(delay * uniform(1 - limits.JOB_BACKOFF_JITTER, 1 + limits.JOB_BACKOFF_JITTER))


def _held_by(job: ClaimedJob, worker_id: str) -> tuple[ColumnElement[bool], ...]:
    # attempts too: each claim of a job is its own, even by the same worker.
    return (
        Job.id == job.id,
        Job.locked_by == worker_id,
        Job.attempts == job.attempts,
        Job.state == JobState.RUNNING,
    )


def _released(state: JobState, **values: Any) -> dict[str, Any]:
    return {"state": state, "locked_by": None, "locked_at": None, **values}


def _change(session: Session, where: tuple[ColumnElement[bool], ...], values: dict) -> bool:
    row = session.execute(
        update(Job)
        .where(*where)
        .values(updated_at=func.now(), **values)
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    ).first()
    return row is not None


def _change_held(
    session: Session, job: ClaimedJob, worker_id: str, step: str, values: dict
) -> bool:
    changed = _change(session, _held_by(job, worker_id), values)
    if not changed:
        _log_lost(job, worker_id, step)
    return changed


def _finish(
    session: Session,
    job: ClaimedJob,
    where: tuple[ColumnElement[bool], ...],
    values: dict,
    reason: str | None,
    fail_visit: FailVisit,
) -> bool:
    """Changes the job and, if it died, fails its visit or its summary: both, or neither if the
    job moved on."""
    try:
        with session.begin_nested():
            # The visit or summary first (the lock order above); the savepoint undoes it if the
            # job moved on.
            if reason is not None and job.visit_id is not None:
                fail_visit(session, job.visit_id, reason)
            if reason is not None and job.summary_id is not None:
                _fail_summary(session, job.summary_id, reason)
            if not _change(session, where, values):
                raise _ClaimLost
    except _ClaimLost:
        return False
    return True


def _fail_summary(session: Session, summary_id: str, reason: str) -> None:
    """A summary whose job died: failed with why, its numbers still shown and Try again
    offered (§9.1). Only while it's being written: a retry has queued a new job by then."""
    session.execute(
        update(Summary)
        .where(Summary.id == summary_id, Summary.status == SummaryStatus.WRITING)
        .values(status=SummaryStatus.FAILED, failure_reason=reason, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


def _log_lost(job: ClaimedJob, worker_id: str, step: str) -> None:
    log.warning(
        "claim lost, nothing changed step=%s %s worker=%s", step, log_fields(job), worker_id
    )


def log_fields(job: ClaimedJob) -> str:
    """job=, kind=, visit= and attempt=: every log line about a job has them, and a summary
    job's summary=."""
    about = f" summary={job.summary_id}" if job.summary_id else ""
    return f"job={job.id} kind={job.kind} visit={job.visit_id}{about} attempt={job.attempts}"
