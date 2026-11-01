"""Summaries asked for, and written by their job (batch 15b; BRIEF D16; CODING_GUIDELINES §2.3,
§2.4, §2.5, §9.1).

- ask: last week's or last month's summary of a team or the org. Asked once: asking again finds
  the same one, and only the ask that made it queues its job (INSERT … ON CONFLICT DO NOTHING).
- page: the period's numbers, counted from SQL now, and its summary if one was asked for. It
  never writes, and never waits for the narrative.
- find: one summary, for the page to poll by its ID until it's written: cheap (nothing is
  counted), and still the same summary after the week or month turns over.
- retry: a failed summary back to writing, with a new job. One that isn't failed is left as it is.
- run, the `summary` job's work: count the numbers, check them (summaries.check), and keep them
  with the summary. A job that dies leaves its summary failed with why (jobs.py).

Lock order: the summary, then its job (jobs.py).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import extraction, jobs, rows, summaries
from countertalk.db import session_scope
from countertalk.errors import Invalid, NotFound, PermanentError
from countertalk.models import (
    JobKind,
    Summary,
    SummaryScope,
    SummaryStatus,
    Team,
    new_id,
    utc_now,
)
from countertalk.playbooks import ORG_PACKS_DIR, PLAYBOOKS_DIR, OrgPack
from countertalk.summaries import OrgNumbers, Period, PeriodKind, TeamNumbers

# Numbers as JSON, for a summary's row and the API.
NUMBERS: TypeAdapter[TeamNumbers | OrgNumbers] = TypeAdapter(TeamNumbers | OrgNumbers)

log = logging.getLogger(__name__)

MISSING = NotFound("summary_not_found", "No such summary")


@dataclass(frozen=True)
class Page:
    """What the summary page shows: the period, its numbers, and its summary if asked for. The
    numbers are the summary's own once it's ready (what its narrative was written from), and
    counted now before then: as_of says when they were counted (R10)."""

    period: Period
    numbers: dict[str, Any]
    as_of: datetime
    summary: Summary | None


def ask(
    session: Session,
    org_id: str,
    scope: str,
    team_id: str | None,
    kind: PeriodKind,
    now: datetime | None = None,
) -> Summary:
    """The summary of the latest whole week or month, made and its job queued if it's new."""
    period = summaries.last_complete(kind, now or utc_now())
    _check_scope(session, org_id, scope, team_id)
    made = session.execute(
        insert(Summary)
        .values(
            id=new_id(),
            org_id=org_id,
            scope=scope,
            team_id=team_id,
            period_start=period.start,
            period_end=period.end,
            status=SummaryStatus.WRITING,
        )
        .on_conflict_do_nothing(
            index_elements=["org_id", "scope", "team_id", "period_start", "period_end"]
        )
        .returning(Summary.id)
    ).scalar_one_or_none()
    if made is not None:
        jobs.enqueue(session, JobKind.SUMMARY, summary_id=made)
        log.info("summary asked summary=%s scope=%s period=%s", made, scope, kind)
    return session.scalars(_of(org_id, scope, team_id, period)).one()


def page(
    session: Session,
    pack_for: Callable[[], OrgPack],
    org_id: str,
    scope: str,
    team_id: str | None,
    kind: PeriodKind,
    now: datetime | None = None,
) -> Page:
    now = now or utc_now()
    period = summaries.last_complete(kind, now)
    _check_scope(session, org_id, scope, team_id)
    found = session.scalars(_of(org_id, scope, team_id, period)).one_or_none()
    if found is not None and found.status == SummaryStatus.READY:
        assert found.numbers is not None and found.counted_at is not None  # ck_summaries_ready
        return Page(period, found.numbers, found.counted_at, found)
    counted = _count(session, pack_for(), org_id, team_id, period)
    return Page(period, NUMBERS.dump_python(counted, mode="json"), now, found)


def find(session: Session, org_id: str, summary_id: str) -> Summary:
    return rows.find_one(session, Summary, org_id, summary_id, MISSING)


def retry(session: Session, org_id: str, summary_id: str) -> Summary:
    """Try again: a failed summary goes back to writing, with a new job."""
    found = rows.find_one(session, Summary, org_id, summary_id, MISSING, for_update=True)
    if found.status == SummaryStatus.FAILED:
        found.status, found.failure_reason = SummaryStatus.WRITING, None
        session.flush()
        jobs.enqueue(session, JobKind.SUMMARY, summary_id=found.id)
        log.info("summary asked again summary=%s", found.id)
    return found


def run(summary_id: str, packs: Path = ORG_PACKS_DIR, playbooks: Path = PLAYBOOKS_DIR) -> None:
    """The summary job's work (module docstring). Raises PermanentError."""
    with session_scope(read_only=True) as session:
        found = session.get(Summary, summary_id)
        if found is None:
            raise MISSING
        if found.status != SummaryStatus.WRITING:
            log.info("summary not being written summary=%s status=%s", summary_id, found.status)
            return
        org_id, team_id = found.org_id, found.team_id
        period = summaries.period_from(found.period_start, found.period_end)
    pack = extraction.pack_for(org_id, packs, playbooks)
    counted_at = utc_now()
    with session_scope(read_only=True) as session:
        numbers = _count(session, pack, org_id, team_id, period)
        problems = summaries.check(session, org_id, numbers)
    for problem in problems:
        log.error("summary numbers don't add up summary=%s problem=%s", summary_id, problem)
    if problems:
        raise PermanentError("summary_numbers_unchecked", "The numbers didn't pass their checks")
    with session_scope() as session:
        found = session.scalars(
            select(Summary)
            .where(Summary.id == summary_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if found.status != SummaryStatus.WRITING:
            return
        found.numbers = NUMBERS.dump_python(numbers, mode="json")
        found.counted_at = counted_at
        found.status = SummaryStatus.READY
    log.info("summary ready summary=%s", summary_id)


def _count(
    session: Session, pack: OrgPack, org_id: str, team_id: str | None, period: Period
) -> TeamNumbers | OrgNumbers:
    if team_id is None:
        return summaries.org_numbers(session, pack, org_id, period)
    return summaries.team_numbers(session, pack, org_id, team_id, period)


def _check_scope(session: Session, org_id: str, scope: str, team_id: str | None) -> None:
    if scope == SummaryScope.ORG and team_id is not None:
        raise Invalid("team_in_org_summary", "The org's summary isn't one team's")
    if scope == SummaryScope.TEAM:
        if team_id is None:
            raise Invalid("team_needed", "Pick the team")
        rows.find_one(session, Team, org_id, team_id, NotFound("team_not_found", "No such team"))


def _of(org_id: str, scope: str, team_id: str | None, period: Period) -> Select[tuple[Summary]]:
    return select(Summary).where(
        Summary.org_id == org_id,
        Summary.scope == scope,
        Summary.team_id.is_not_distinct_from(team_id),
        Summary.period_start == period.start,
        Summary.period_end == period.end,
    )
