"""/api/summaries (batch 15b): a team's or the org's summary of last week or last month. The
page reads its numbers at once (GET, from SQL, never waiting for the narrative), asks for the
summary (POST: one per team and period, however often it's asked), and polls it by its ID until
it's ready (GET /{id}, which counts nothing). Try again (POST …/retry) queues a new job for a
failed one. Anyone in the org may ask, for now: batch 16 adds who may read which team's."""

from datetime import date, datetime
from functools import partial
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from countertalk import drill, extraction, summaries, summary_jobs
from countertalk.api.deps import CurrentUser
from countertalk.api.insights import InsightOut, insight_out
from countertalk.db import DbSession
from countertalk.models import Summary, SummaryScope, utc_now
from countertalk.narrative import Narrative
from countertalk.summaries import PeriodKind, Totals

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


class DrillVisitOut(BaseModel):
    visit_id: str
    visited_at: datetime
    # In the summary's period, or before it in the issue's run.
    in_period: bool
    insights: list[InsightOut]


class DrillShopOut(BaseModel):
    shop_id: str
    name: str
    area: str
    distributor: str | None
    visits: list[DrillVisitOut]


class DrillRepOut(BaseModel):
    """A rep's shops in the issue, and how many insights: never ranked or compared."""

    rep_id: str
    rep_name: str
    insights: int
    shops: list[DrillShopOut]


class DrillOut(BaseModel):
    """A team summary's issue drilled down (batch 15c): its latest run up to the period's end,
    counted now (numbers_as_of; a summary written earlier keeps its own). shops is its open
    shops while it's open; in_period is the summary's own shops, insights, reps and visits."""

    period: PeriodKind
    period_start: datetime
    period_end: datetime
    numbers_as_of: datetime
    issue_id: str
    title_en: str
    category: str
    # As it was at the period's end.
    status: str
    shops: int
    insights: int
    reps: int
    raised_again: int
    in_period: Totals
    by_rep: list[DrillRepOut]


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


@router.get("/drill")
def get_drill(
    team_id: UUID,
    issue_id: UUID,
    period: PeriodKind,
    user: CurrentUser,
    session: DbSession,
    start: date | None = None,
) -> DrillOut:
    """The team's issue in its summary of a week or month, drilled down to each quote: the one
    starting on `start` (its first day in IST, as the summary page linked it), else the latest
    whole one. 400 not_a_period_start; 404 issue_not_found if it isn't one of the team's issues
    that summary lists."""
    now = utc_now()
    found = drill.drill(
        session,
        user.org_id,
        str(team_id),
        str(issue_id),
        summaries.period_starting(period, start) if start else summaries.last_complete(period, now),
        now,
    )
    return DrillOut(
        period=period,
        period_start=found.period.start,
        period_end=found.period.end,
        numbers_as_of=found.as_of,
        issue_id=found.issue.id,
        title_en=found.issue.title_en,
        category=found.issue.category,
        status=found.status,
        shops=found.shops,
        insights=found.insights,
        reps=found.reps,
        raised_again=found.raised_again,
        in_period=found.in_period,
        by_rep=[
            DrillRepOut(
                rep_id=rep.rep_id,
                rep_name=rep.rep_name,
                insights=rep.insights,
                shops=[
                    DrillShopOut(
                        shop_id=shop.shop_id,
                        name=shop.name,
                        area=shop.area,
                        distributor=shop.distributor,
                        visits=[
                            DrillVisitOut(
                                visit_id=visit.visit_id,
                                visited_at=visit.visited_at,
                                in_period=visit.in_period,
                                insights=[insight_out(view) for view in visit.insights],
                            )
                            for visit in shop.visits
                        ],
                    )
                    for shop in rep.shops
                ],
            )
            for rep in found.by_rep
        ],
    )


@router.get("/{summary_id}")
def get_summary(summary_id: UUID, user: CurrentUser, session: DbSession) -> SummaryOut:
    return summary_out(summary_jobs.find(session, user.org_id, str(summary_id)))


@router.post("/{summary_id}/retry")
def retry_summary(summary_id: UUID, user: CurrentUser, session: DbSession) -> SummaryOut:
    """Try again: a failed summary is written again by a new job. Any other is left as it is."""
    return summary_out(summary_jobs.retry(session, user.org_id, str(summary_id)))


def _id(team_id: UUID | None) -> str | None:
    return None if team_id is None else str(team_id)
