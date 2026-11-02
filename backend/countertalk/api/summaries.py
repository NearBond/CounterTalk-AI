"""/api/summaries (batch 15b): a team's or the org's summary of last week or last month. The
page reads its numbers at once (GET, from SQL, never waiting for the narrative), asks for the
summary (POST: one per team and period, however often it's asked), and polls it by its ID until
it's ready (GET /{id}, which counts nothing). Try again (POST …/retry) queues a new job for a
failed one. Anyone in the org may ask, for now: batch 16 adds who may read which team's."""

from datetime import datetime
from functools import partial
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from countertalk import extraction, summary_jobs
from countertalk.api.deps import CurrentUser
from countertalk.db import DbSession
from countertalk.models import Summary, SummaryScope
from countertalk.narrative import Narrative
from countertalk.summaries import PeriodKind

router = APIRouter(prefix="/api/summaries", tags=["summaries"])


class SummaryAsk(BaseModel):
    """period: last week or last month, the latest whole one."""

    model_config = ConfigDict(extra="forbid")

    scope: SummaryScope
    team_id: UUID | None = None
    period: PeriodKind


class SummaryOut(BaseModel):
    id: str
    scope: str
    team_id: str | None
    period_start: datetime
    period_end: datetime
    # writing, ready or failed (with failure_reason, and Try again).
    status: str
    # Once it's ready: the numbers its narrative was written from, and when they were counted.
    numbers: dict[str, Any] | None
    counted_at: datetime | None
    narrative: Narrative | None
    model: str | None
    failure_reason: str | None


class SummaryPageOut(BaseModel):
    """The period's numbers: the summary's own once it's ready (what its narrative was written
    from), counted now before then. numbers_as_of says when they were counted."""

    period: PeriodKind
    period_start: datetime
    period_end: datetime
    numbers: dict[str, Any]
    numbers_as_of: datetime
    summary: SummaryOut | None


def summary_out(found: Summary) -> SummaryOut:
    return SummaryOut.model_validate(found, from_attributes=True)


@router.get("")
def get_summary_page(
    scope: SummaryScope,
    period: PeriodKind,
    user: CurrentUser,
    session: DbSession,
    team_id: UUID | None = None,
) -> SummaryPageOut:
    pack = partial(extraction.pack_for, user.org_id, session=session)
    found = summary_jobs.page(session, pack, user.org_id, scope, _id(team_id), period)
    return SummaryPageOut(
        period=period,
        period_start=found.period.start,
        period_end=found.period.end,
        numbers=found.numbers,
        numbers_as_of=found.as_of,
        summary=None if found.summary is None else summary_out(found.summary),
    )


@router.post("")
def ask_summary(body: SummaryAsk, user: CurrentUser, session: DbSession) -> SummaryOut:
    """The summary, asked for: made, with its job queued, only the first time."""
    found = summary_jobs.ask(session, user.org_id, body.scope, _id(body.team_id), body.period)
    return summary_out(found)


@router.get("/{summary_id}")
def get_summary(summary_id: UUID, user: CurrentUser, session: DbSession) -> SummaryOut:
    return summary_out(summary_jobs.find(session, user.org_id, str(summary_id)))


@router.post("/{summary_id}/retry")
def retry_summary(summary_id: UUID, user: CurrentUser, session: DbSession) -> SummaryOut:
    """Try again: a failed summary is written again by a new job. Any other is left as it is."""
    return summary_out(summary_jobs.retry(session, user.org_id, str(summary_id)))


def _id(team_id: UUID | None) -> str | None:
    return None if team_id is None else str(team_id)
