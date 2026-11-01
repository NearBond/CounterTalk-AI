"""/api/issues: a team's inbox (batch 15). The team is the `team` query parameter, its key: the
dev dropdown's until batch 16 takes it from the signed-in member. GET /api/issues/teams lists
the org's teams for that dropdown and for Move and triage."""

from datetime import datetime
from functools import partial
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictBool, StrictInt

from countertalk import extraction, inbox, issues
from countertalk.api.deps import CurrentUser
from countertalk.api.insights import InsightOut, RouteOut, insight_out
from countertalk.db import DbSession

router = APIRouter(prefix="/api/issues", tags=["issues"])

# The one place the API reads the team (batch 16 swaps it for the signed-in member's).
TeamKey = Annotated[str, Query(min_length=1, description="The team's key (dev only)")]


class TeamOut(BaseModel):
    id: str
    key: str
    name: str


class IssueOut(BaseModel):
    """A card. Counts are of the issue's insights' shops, insights and reps; raised_again: a
    shop with insights from two or more visits in it."""

    id: str
    team_key: str
    category: str
    title_en: str
    severity: str
    status: str
    owner_id: str | None
    owner_name: str | None
    due_at: datetime
    overdue: bool
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    resolution_note: str | None
    reopened_count: int
    shops: int
    insights: int
    reps: int
    raised_again: bool
    # What a change names (compare-and-set): 409 issue_changed if it moved on.
    version: int


class MemberOut(BaseModel):
    """One insight of an issue: its shop, rep and visit, and its route in this issue's team."""

    insight: InsightOut
    shop_name: str
    area: str
    rep_name: str
    visit_id: str
    visited_at: datetime
    route: RouteOut


class IssueDetailOut(BaseModel):
    issue: IssueOut
    members: list[MemberOut]


class IssueChange(BaseModel):
    """PATCH: the version read, and any of these. Resolving needs a note; only the owner
    changes the due date (403 not_owner)."""

    model_config = ConfigDict(extra="forbid")

    version: StrictInt
    status: Literal["open", "in_progress", "resolved"] | None = None
    note: str | None = None
    assign_to_me: StrictBool = False
    due_at: AwareDatetime | None = None


class UntriagedOut(BaseModel):
    insight: InsightOut
    shop_name: str
    area: str
    rep_name: str
    visit_id: str
    visited_at: datetime


def issue_out(card: inbox.Card) -> IssueOut:
    found = card.issue
    return IssueOut(
        id=found.id,
        team_key=card.team_key,
        category=found.category,
        title_en=found.title_en,
        severity=found.severity,
        status=found.status,
        owner_id=found.owner_id,
        owner_name=card.owner_name,
        due_at=found.due_at,
        overdue=card.overdue,
        first_seen_at=found.first_seen_at,
        last_seen_at=found.last_seen_at,
        resolved_at=found.resolved_at,
        resolution_note=found.resolution_note,
        reopened_count=found.reopened_count,
        shops=card.shops,
        insights=card.insights,
        reps=card.reps,
        raised_again=card.raised_again,
        version=found.version,
    )


def detail_out(found: inbox.Detail) -> IssueDetailOut:
    return IssueDetailOut(
        issue=issue_out(found.card),
        members=[
            MemberOut(
                insight=insight_out(member.insight),
                shop_name=member.shop_name,
                area=member.area,
                rep_name=member.rep_name,
                visit_id=member.visit_id,
                visited_at=member.visited_at,
                route=RouteOut.model_validate(member.route, from_attributes=True),
            )
            for member in found.members
        ],
    )


@router.get("/teams")
def list_teams(user: CurrentUser, session: DbSession) -> list[TeamOut]:
    return [TeamOut(id=t.id, key=t.key, name=t.name) for t in inbox.teams(session, user.org_id)]


@router.get("/triage")
def list_triage(user: CurrentUser, session: DbSession) -> list[UntriagedOut]:
    """Confirmed insights with no team, for someone to assign one."""
    return [
        UntriagedOut(
            insight=insight_out(item.insight),
            shop_name=item.shop_name,
            area=item.area,
            rep_name=item.rep_name,
            visit_id=item.visit_id,
            visited_at=item.visited_at,
        )
        for item in inbox.untriaged(session, user.org_id)
    ]


@router.get("")
def list_issues(
    team: TeamKey,
    user: CurrentUser,
    session: DbSession,
    status: Annotated[list[Literal["open", "in_progress", "resolved"]] | None, Query()] = None,
    category: str | None = None,
    area: str | None = None,
    distributor: UUID | None = None,
) -> list[IssueOut]:
    """The team's issues: open and in progress unless `status` says otherwise; highest severity
    first, then most shops, then the most recently seen."""
    found = inbox.team_of(session, user.org_id, team)
    filters = inbox.Filters(
        statuses=tuple(status) if status else inbox.NO_FILTERS.statuses,
        category=category,
        area=area,
        distributor_id=str(distributor) if distributor else None,
    )
    pack = partial(extraction.pack_for, user.org_id, session=session)
    return [issue_out(card) for card in inbox.cards(session, pack, user.org_id, found.id, filters)]


@router.get("/{issue_id}")
def get_issue(issue_id: UUID, user: CurrentUser, session: DbSession) -> IssueDetailOut:
    return detail_out(inbox.detail(session, user.org_id, str(issue_id)))


@router.patch("/{issue_id}")
def change_issue(
    issue_id: UUID, body: IssueChange, user: CurrentUser, session: DbSession
) -> IssueDetailOut:
    """409 issue_changed when the version read is out of date; nothing is written then."""
    issues.change(
        session,
        user.org_id,
        user.id,
        str(issue_id),
        body.version,
        status=body.status,
        note=body.note,
        assign_to_me=body.assign_to_me,
        due_at=body.due_at,
    )
    return detail_out(inbox.detail(session, user.org_id, str(issue_id)))
