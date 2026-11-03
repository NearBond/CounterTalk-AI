"""Retention (batch 17): the daily `retention` job, which housekeeping queues.

- **Audio:** each visit confirmed more than AUDIO_RETENTION_DAYS ago loses its recording
  (storage.remove_audio). Its transcript, quotes and speech-to-text replies stay, and ▶ says
  the audio was deleted under the retention policy. One visit at a time: audio_deleted_at is
  set and committed under the visit's lock first, and only then are its files deleted (§9.3).
  A run that stops between the two leaves a visit marked with its files still there, and a
  run in the next RETRY_DAYS deletes them: it deletes the files of every visit marked that
  recently.
- **Only confirmed visits** (§9.7): the query asks for them, the lock reads the visit fresh, and
  the database's CHECK refuses audio_deleted_at on any other.
- **Old jobs:** done ones after DONE_JOB_KEEP_DAYS, dead ones after DEAD_JOB_KEEP_DAYS, by the
  database's clock, like every job time. A dead job's visit keeps its failure_reason.
- A run whose claim is lost stops at once: the worker that has the job now does the rest.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from countertalk import jobs, limits, storage, visits
from countertalk.config import get_settings
from countertalk.db import session_scope
from countertalk.models import ACTIVE_JOB, Job, JobKind, JobState, Visit, VisitStatus, utc_now

log = logging.getLogger(__name__)
# Two workers' housekeeping at once: one queues, the other then sees its job.
_QUEUE_LOCK = 1_717_000_017
# How far back a run looks for marked visits whose files are still there.
RETRY_DAYS = 7


def queue_due(session: Session) -> bool:
    """Queues the retention job, due in RETENTION_DELAY_MS, unless one is active or one was
    queued within the day."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _QUEUE_LOCK})
    since = func.now() - timedelta(milliseconds=limits.RETENTION_EVERY_MS)
    recent = session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == JobKind.RETENTION, or_(text(ACTIVE_JOB), Job.created_at > since))
    )
    if recent:
        return False
    job_id = jobs.enqueue(session, JobKind.RETENTION)
    if job_id is None:
        return False
    due = func.now() + timedelta(milliseconds=limits.RETENTION_DELAY_MS)
    session.execute(update(Job).where(Job.id == job_id).values(run_after=due))
    return True


@dataclass(frozen=True)
class Swept:
    visits: int  # whose audio this run marked deleted
    files: int  # removed, for those and any marked before whose files were left
    done_jobs: int
    dead_jobs: int


def run(claim_lost: threading.Event | None = None, now: datetime | None = None) -> Swept:
    now = now or utc_now()
    cutoff = now - timedelta(days=get_settings().AUDIO_RETENTION_DAYS)
    with session_scope(read_only=True) as session:
        due = session.scalars(
            select(Visit.id)
            .where(
                Visit.status == VisitStatus.CONFIRMED,
                Visit.confirmed_at <= cutoff,
                Visit.audio_deleted_at.is_(None),
            )
            .order_by(Visit.confirmed_at, Visit.id)
        ).all()
    marked = 0
    for visit_id in due:
        if _lost(claim_lost):
            return Swept(marked, 0, 0, 0)
        with session_scope() as session:  # one visit per transaction (R6)
            visit = visits.lock_for_work(session, visit_id)
            if (
                visit is None
                or visit.status != VisitStatus.CONFIRMED
                or visit.audio_deleted_at is not None
                or visit.confirmed_at is None
                or visit.confirmed_at > cutoff
            ):
                continue
            visit.audio_deleted_at = now
        marked += 1
        log.info("audio deleted under the retention policy visit=%s", visit_id)
    # Files only once the marks are committed (§9.3): this run's, and any an earlier run marked
    # but stopped before deleting.
    with session_scope(read_only=True) as session:
        recently = Visit.audio_deleted_at >= now - timedelta(days=RETRY_DAYS)
        gone = session.scalars(select(Visit.id).where(recently)).all()
    files = 0
    for visit_id in gone:
        if _lost(claim_lost):
            return Swept(marked, files, 0, 0)
        files += storage.remove_audio(visit_id)
    with session_scope() as session:
        done = _prune(session, JobState.DONE, limits.DONE_JOB_KEEP_DAYS)
        dead = _prune(session, JobState.DEAD, limits.DEAD_JOB_KEEP_DAYS)
    swept = Swept(marked, files, done, dead)
    log.info(
        "retention visits=%d files=%d done_jobs=%d dead_jobs=%d",
        swept.visits,
        swept.files,
        swept.done_jobs,
        swept.dead_jobs,
    )
    return swept


def _lost(claim_lost: threading.Event | None) -> bool:
    return claim_lost is not None and claim_lost.is_set()


def _prune(session: Session, state: str, days: int) -> int:
    """Jobs left in this state for more than these days: how many went."""
    before = func.now() - timedelta(days=days)
    removed = session.execute(delete(Job).where(Job.state == state, Job.updated_at < before))
    return removed.rowcount
