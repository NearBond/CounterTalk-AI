"""Summaries asked for, and written by their job (batch 15b; BRIEF D16; CODING_GUIDELINES §2.3,
§2.4, §2.5, §9.1).

- ask: last week's or last month's summary of a team or the org. Asked once: asking again finds
  the same one, and only the ask that made it queues its job (INSERT … ON CONFLICT DO NOTHING).
- page: the period's numbers, counted from SQL now, and its summary if one was asked for. It
  never writes, and never waits for the narrative.
- find: one summary, for the page to poll by its ID until it's written: cheap (nothing is
  counted), and still the same summary after the week or month turns over.
- retry: a failed summary back to writing, with a new job. One that isn't failed is left as it is.
- run, the `summary` job's work: count the numbers and check them (summaries.check); for a
  team's, ask once for merge suggestions (merges.py, batch 15c); have Muse Spark write the
  narrative and check its sentences (narrative.py), then keep both with the summary. A job
  that dies leaves its summary failed with why (jobs.py).

Lock order: the summary, then its job (jobs.py).
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import extraction, jobs, llm_calls, merges, narrative, rows, summaries
from countertalk.db import session_scope
from countertalk.errors import DomainError, Invalid, NotFound, PermanentError
from countertalk.llm.base import StructuredLlm
from countertalk.models import (
    JobKind,
    LlmPurpose,
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
    found = existing(session, org_id, scope, team_id, period)
    if found is not None and found.status == SummaryStatus.READY:
        assert found.numbers is not None and found.counted_at is not None  # ck_summaries_ready
        return Page(period, found.numbers, found.counted_at, found)
    counted = count(session, pack_for(), org_id, team_id, period)
    return Page(period, NUMBERS.dump_python(counted, mode="json"), now, found)


def find(session: Session, org_id: str, summary_id: str) -> Summary:
    return rows.find_one(session, Summary, org_id, summary_id, _missing())


def retry(session: Session, org_id: str, summary_id: str) -> Summary:
    """Try again: a failed summary goes back to writing, with a new job."""
    found = rows.find_one(session, Summary, org_id, summary_id, _missing(), for_update=True)
    if found.status == SummaryStatus.FAILED:
        found.status, found.failure_reason = SummaryStatus.WRITING, None
        session.flush()
        jobs.enqueue(session, JobKind.SUMMARY, summary_id=found.id)
        log.info("summary asked again summary=%s", found.id)
    return found


def run(
    summary_id: str,
    llm: StructuredLlm | None,
    *,
    claim_lost: threading.Event | None = None,
    packs: Path = ORG_PACKS_DIR,
    playbooks: Path = PLAYBOOKS_DIR,
) -> None:
    """The summary job's work (module docstring). Raises PermanentError or RetryableError."""
    with session_scope(read_only=True) as session:
        found = session.get(Summary, summary_id)
        if found is None:
            raise _missing()
        if found.status != SummaryStatus.WRITING:
            log.info("summary not being written summary=%s status=%s", summary_id, found.status)
            return
        org_id, team_id = found.org_id, found.team_id
        period = summaries.period_from(found.period_start, found.period_end)
        stepped = found.merge_step is not None
    pack = extraction.pack_for(org_id, packs, playbooks)
    counted_at = utc_now()
    with session_scope(read_only=True) as session:
        numbers = count(session, pack, org_id, team_id, period)
        problems = summaries.check(session, org_id, numbers)
    for problem in problems:
        log.error("summary numbers don't add up summary=%s problem=%s", summary_id, problem)
    if problems:
        raise PermanentError("summary_numbers_unchecked", "The numbers didn't pass their checks")
    if team_id is not None and not stepped and llm is not None:
        # Once, before the narrative: a retried job doesn't ask (or pay) again.
        _keep_step(summary_id, _merge_step(summary_id, llm, claim_lost, pack, org_id, team_id))
    numbers_json = NUMBERS.dump_python(numbers, mode="json")
    written = _narrative(summary_id, llm, claim_lost, pack, org_id, numbers)
    with session_scope() as session:
        found = _locked(session, summary_id)
        if found.status != SummaryStatus.WRITING:
            return
        found.numbers, found.counted_at = numbers_json, counted_at
        found.narrative = written
        # The model asked: none when there was nothing to cite.
        found.model = llm.model if written is not None and llm is not None else None
        found.status = SummaryStatus.READY
    log.info("summary ready summary=%s", summary_id)


def _merge_step(
    summary_id: str,
    llm: StructuredLlm,
    claim_lost: threading.Event | None,
    pack: OrgPack,
    org_id: str,
    team_id: str,
) -> dict[str, Any]:
    """merges.suggest, which never fails the summary: an extra (merges.py). A lost claim
    still stops the job; anything else is logged (its frames, never its words) and kept as
    the step's error, so a retry doesn't ask again."""
    failed = "merge suggestions failed summary=%s code=%s"
    try:
        return merges.suggest(summary_id, llm, claim_lost, pack, org_id, team_id)
    except DomainError as error:
        if error.code == "claim_lost":
            raise
        log.warning(failed, summary_id, error.code, exc_info=True)
        return {"error": error.code}
    except Exception:
        log.warning(failed, summary_id, "unexpected_error", exc_info=True)
        return {"error": "unexpected_error"}


def _locked(session: Session, summary_id: str) -> Summary:
    """The job's own summary, locked and read fresh (R6)."""
    return session.scalars(
        select(Summary)
        .where(Summary.id == summary_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def _keep_step(summary_id: str, step: dict[str, Any]) -> None:
    """The merge suggestion step's counts, kept with the summary while it's being written."""
    with session_scope() as session:
        found = _locked(session, summary_id)
        if found.status == SummaryStatus.WRITING:
            found.merge_step = step


def _narrative(
    summary_id: str,
    llm: StructuredLlm | None,
    claim_lost: threading.Event | None,
    pack: OrgPack,
    org_id: str,
    numbers: TeamNumbers | OrgNumbers,
) -> narrative.Narrative | None:
    """The narrative as checked, or None with nothing to cite (no call is made then). Without
    an LLM (no MODEL_API_KEY), a summary with something to cite fails, and Try again works once
    there's one. Logs only counts: never its words or quotes (§1.9)."""
    with session_scope(read_only=True) as session:
        request = narrative.request_for(session, pack, org_id, numbers)
    if not request.offered:
        log.info("summary has nothing to cite, so only numbers summary=%s", summary_id)
        return None
    if llm is None:
        raise PermanentError(
            "llm_not_configured",
            "The summary's words need Muse Spark, which isn't set up on the server (MODEL_API_KEY)",
        )
    log.info("summary narrative asked summary=%s quotes=%d", summary_id, len(request.offered))
    outcome = llm.complete(request.messages, narrative.NAME, request.schema, claim_lost=claim_lost)
    llm_calls.record(None, LlmPurpose.SUMMARY, llm.provider, outcome, summary_id=summary_id)
    invalid = PermanentError(
        "summary_invalid", "Muse Spark's answer didn't fit, even after a repair"
    )
    answer = llm_calls.answer_of(outcome, invalid)
    with session_scope(read_only=True) as session:
        written = narrative.checked(session, org_id, numbers, request, answer)
    dropped = " ".join(f"dropped_{why}={n}" for why, n in written["dropped"].items())
    log.info(
        "summary narrative checked summary=%s kept=%d %s", summary_id, written["kept"], dropped
    )
    return written


def _missing() -> NotFound:
    """A fresh one each time: an error raised again keeps its old frames (R2)."""
    return NotFound("summary_not_found", "No such summary")


def count(
    session: Session, pack: OrgPack, org_id: str, team_id: str | None, period: Period
) -> TeamNumbers | OrgNumbers:
    """A team's numbers for the period, or the org's (no team)."""
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


def existing(
    session: Session, org_id: str, scope: str, team_id: str | None, period: Period
) -> Summary | None:
    """The summary of this scope, team and period, if one was asked for."""
    return session.scalars(_of(org_id, scope, team_id, period)).one_or_none()


def _of(org_id: str, scope: str, team_id: str | None, period: Period) -> Select[tuple[Summary]]:
    return select(Summary).where(
        Summary.org_id == org_id,
        Summary.scope == scope,
        Summary.team_id.is_not_distinct_from(team_id),
        Summary.period_start == period.start,
        Summary.period_end == period.end,
    )
