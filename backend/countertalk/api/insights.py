"""/api/insights: a rep's edits to one insight during review (batch 14), and the shapes the
review page reads (GET /api/visits/{id}/review lives with the visit's routes)."""

from datetime import date, datetime
from functools import partial
from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, StrictInt

from countertalk import extraction, review
from countertalk.api.deps import CurrentUser
from countertalk.db import DbSession

router = APIRouter(prefix="/api/insights", tags=["insights"])


class QuoteOut(BaseModel):
    seq: int
    turn_id: int | None
    speaker_role: str | None
    # The transcript's own words where it was found; the model's where it wasn't.
    text: str
    match: str | None
    # Where ▶ starts playing; null for a quote that wasn't found.
    start_ms: int | None


class RouteOut(BaseModel):
    slot: int
    team_key: str
    team_name: str
    routed_by: str


class InsightOut(BaseModel):
    id: str
    seq: int
    kind: str
    category: str
    severity: str
    summary_en: str
    verified: bool
    visible: bool
    added_by_rep: bool
    sku: str | None
    competitor: str | None
    scheme_key: str | None
    ai_team: str | None
    ai_team_reason: str | None
    quotes: list[QuoteOut]
    routes: list[RouteOut]


class InsightChange(BaseModel):
    """PATCH: any of the three; a category or severity change re-routes it."""

    model_config = ConfigDict(extra="forbid")

    summary_en: str | None = None
    category: str | None = None
    severity: str | None = None


class NewInsight(BaseModel):
    """POST /api/visits/{id}/insights: a finding the AI missed, quoting a whole turn."""

    model_config = ConfigDict(extra="forbid")

    turn_id: StrictInt
    kind: str
    category: str
    severity: str
    summary_en: str


class NewNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class NoteOut(BaseModel):
    id: str
    text: str
    created_at: datetime


class ReportOut(BaseModel):
    summary_en: str
    next_visit_date: date | None


class TaskOut(BaseModel):
    seq: int
    type: str
    description_en: str
    due: date | None
    owner: str
    status: str


class ChoiceOut(BaseModel):
    key: str
    label: str


def insight_out(view: review.InsightView) -> InsightOut:
    found = view.insight
    return InsightOut(
        id=found.id,
        seq=found.seq,
        kind=found.kind,
        category=found.category,
        severity=found.severity,
        summary_en=found.summary_en,
        verified=found.verified,
        visible=found.visible,
        added_by_rep=found.added_by_rep,
        sku=view.sku,
        competitor=view.competitor,
        scheme_key=found.scheme_key,
        ai_team=view.ai_team,
        ai_team_reason=found.ai_team_reason,
        quotes=[QuoteOut.model_validate(quote, from_attributes=True) for quote in view.quotes],
        routes=[RouteOut.model_validate(route, from_attributes=True) for route in view.routes],
    )


@router.patch("/{insight_id}")
def edit_insight(
    insight_id: UUID, body: InsightChange, user: CurrentUser, session: DbSession
) -> InsightOut:
    """Only in review; 409 insight_unverified for one that couldn't be found in the recording."""
    changes = body.model_dump(exclude_none=True)
    pack = partial(extraction.pack_for, user.org_id)
    found = review.edit_insight(session, pack, user.org_id, user.id, str(insight_id), **changes)
    [view] = review.insight_views(session, [found])
    return insight_out(view)


@router.delete("/{insight_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_insight(insight_id: UUID, user: CurrentUser, session: DbSession) -> Response:
    review.delete_insight(session, user.org_id, user.id, str(insight_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
