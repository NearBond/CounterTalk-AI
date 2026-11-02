"""/api/insights: a rep's edits to one insight during review (batch 14), and the shapes the
review page reads (GET /api/visits/{id}/review lives with the visit's routes)."""

from datetime import date, datetime
from functools import partial
from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, StrictInt

from countertalk import access, extraction, review, routing
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
    team_id: str
    team_key: str
    team_name: str
    routed_by: str
    # What a team member's accept, Move or Also send names (compare-and-set).
    version: int
    # Its team's issue, once its visit is confirmed (batch 15): its status and resolution note
    # are what the rep tells the shop. Null for a route confirmed before issues existed.
    issue_id: str | None
    issue_status: str | None
    resolution_note: str | None


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
    """POST /api/visits/{id}/insights: a finding the AI missed, quoting a whole turn. The client
    makes its ID, so sending it again is safe (§2.5)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    turn_id: StrictInt
    kind: str
    category: str
    severity: str
    summary_en: str


class NewNote(BaseModel):
    """The client makes its ID, so sending it again is safe (§2.5)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
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


class OrderItemOut(BaseModel):
    """What the shop ordered, as said; `sku` is the catalog item it links to, if it does."""

    seq: int
    name: str
    quantity: float | None
    unit: str | None
    sku: str | None


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
    access.insight(session, user, str(insight_id), change=True)
    changes = body.model_dump(exclude_none=True)
    pack = partial(extraction.pack_for, user.org_id, session=session)
    found = review.edit_insight(session, pack, user.org_id, user.id, str(insight_id), **changes)
    [view] = review.insight_views(session, [found])
    return insight_out(view)


@router.delete("/{insight_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_insight(insight_id: UUID, user: CurrentUser, session: DbSession) -> Response:
    access.insight(session, user, str(insight_id), change=True)
    review.delete_insight(session, user.org_id, user.id, str(insight_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- a team member's routing, after confirm (batch 15) ---------------------------------------


class RouteSeen(BaseModel):
    """The route acted on, as read: 409 route_changed if it has moved on."""

    model_config = ConfigDict(extra="forbid")

    slot: StrictInt
    version: StrictInt


class RouteTo(RouteSeen):
    team_id: UUID


class TeamFor(BaseModel):
    """From triage: read with no routes, so 409 already_routed if someone gave it one."""

    model_config = ConfigDict(extra="forbid")

    team_id: UUID


@router.post("/{insight_id}/accept")
def accept(insight_id: UUID, body: RouteSeen, user: CurrentUser, session: DbSession) -> InsightOut:
    """The AI's suggested team, accepted: no longer "suggested by AI"."""
    access.route(session, user, str(insight_id), body.slot)
    seen = routing.Seen(body.slot, body.version)
    route = routing.accept(session, user.org_id, str(insight_id), user.id, seen)
    return insight_out(review.view_of(session, user.org_id, route.insight_id))


@router.post("/{insight_id}/move")
def move(insight_id: UUID, body: RouteTo, user: CurrentUser, session: DbSession) -> InsightOut:
    """The route goes to another team's issue."""
    access.route(session, user, str(insight_id), body.slot)
    pack = partial(extraction.pack_for, user.org_id, session=session)
    seen = routing.Seen(body.slot, body.version)
    route = routing.move(
        session, pack, user.org_id, str(insight_id), str(body.team_id), user.id, seen
    )
    return insight_out(review.view_of(session, user.org_id, route.insight_id))


@router.post("/{insight_id}/also-send")
def also_send(insight_id: UUID, body: RouteTo, user: CurrentUser, session: DbSession) -> InsightOut:
    """It also goes to another team: 409 team_limit at two."""
    access.route(session, user, str(insight_id), body.slot)
    pack = partial(extraction.pack_for, user.org_id, session=session)
    seen = routing.Seen(body.slot, body.version)
    route = routing.also_send(
        session, pack, user.org_id, str(insight_id), str(body.team_id), user.id, seen
    )
    return insight_out(review.view_of(session, user.org_id, route.insight_id))


@router.post("/{insight_id}/team")
def assign_from_triage(
    insight_id: UUID, body: TeamFor, user: CurrentUser, session: DbSession
) -> InsightOut:
    """A team for an insight in triage."""
    access.triage(session, user, str(insight_id))
    pack = partial(extraction.pack_for, user.org_id, session=session)
    route = routing.assign_from_triage(
        session, pack, user.org_id, str(insight_id), str(body.team_id), user.id
    )
    return insight_out(review.view_of(session, user.org_id, route.insight_id))
