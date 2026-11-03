"""/api/issues: a team's inbox (batch 15). A team member's is their own team's; a manager or
admin names a team by its key, `team` (access.team). GET /api/issues/options gives the teams
(the managers' picker, Move's and triage's), and the filters' choices."""

from datetime import date, datetime
from functools import partial
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictBool, StrictInt
from sqlalchemy.orm import Session

from countertalk import access, extraction, inbox, issues, merges
from countertalk.api.deps import CurrentUser
from countertalk.api.insights import ChoiceOut, InsightOut, RouteOut, insight_out
from countertalk.db import DbSession

router = APIRouter(prefix="/api/issues", tags=["issues"])

# A manager's or admin's pick; a team member's is their own, whatever this says (access.team).
TeamKey = Annotated[
    str | None, Query(min_length=1, description="The team's key: a manager's or admin's pick")
]


class TeamOut(BaseModel):
    id: str
    key: str
    name: str


class NamedOut(BaseModel):
    """A picker's choice by ID and name: a distributor, a competitor or a product (batch 17c)."""

    id: str
    name: str


class PeriodOut(BaseModel):
    """A ready-made period (batch 17c): `from` and `to` as GET /api/issues takes them."""

    key: str
    label: str
    first_day: date
    last_day: date


class OptionsOut(BaseModel):
    """What the inbox's pickers offer: the teams (a manager's picker, Move and triage), and the
    filters' categories, areas, distributors, competitors and products (only those some
    issue's insight links to), and the period picker's ready-made periods."""

    teams: list[TeamOut]
    categories: list[ChoiceOut]
    areas: list[str]
    distributors: list[NamedOut]
    competitors: list[NamedOut]
    products: list[NamedOut]
    periods: list[PeriodOut]


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


class OtherIssueOut(BaseModel):
    """The other issue of a merge suggestion, as it is now: its version is what Merge names."""

    id: str
    title_en: str
    version: int


class SuggestionOut(BaseModel):
    """An open merge suggestion (batch 15c): "These look like the same issue: <reason>". The
    issue kept is into_issue_id; the other one is merged into it."""

    id: str
    into_issue_id: str
    merge_issue_id: str
    reason: str
    other: OtherIssueOut


class IssueDetailOut(BaseModel):
    issue: IssueOut
    members: list[MemberOut]
    # Its open merge suggestions (batch 15c), whose two issues are both still active.
    suggestions: list[SuggestionOut]


class MergeIn(BaseModel):
    """Merge: both issues' versions as read. 409 issue_changed if either moved on."""

    model_config = ConfigDict(extra="forbid")

    into_version: StrictInt
    merge_version: StrictInt


class IssueChange(BaseModel):
    """PATCH: the version read, and any of these. Resolving needs a note; only the owner
    changes the due date (403 not_owner)."""

    model_config = ConfigDict(extra="forbid")

    version: StrictInt
    status: Literal["open", "in_progress", "resolved"] | None = None
    note: str | None = None
    assign_to_me: StrictBool = False
    due_at: AwareDatetime | None = None


class FindingOut(BaseModel):
    """A confirmed insight with its shop, rep and visit: triage's, and an answer's (batch 17d)."""

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


def finding_out(item: inbox.Finding) -> FindingOut:
    return FindingOut(
        insight=insight_out(item.insight),
        shop_name=item.shop_name,
        area=item.area,
        rep_name=item.rep_name,
        visit_id=item.visit_id,
        visited_at=item.visited_at,
    )


def detail_of(session: Session, org_id: str, issue_id: str) -> IssueDetailOut:
    """The issue, its insights, and its open merge suggestions."""
    return detail_out(
        inbox.detail(session, org_id, issue_id), merges.open_for(session, org_id, issue_id)
    )


def detail_out(found: inbox.Detail, suggested: list[merges.Open]) -> IssueDetailOut:
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
        suggestions=[
            SuggestionOut(
                id=s.suggestion.id,
                into_issue_id=s.suggestion.into_issue_id,
                merge_issue_id=s.suggestion.merge_issue_id,
                reason=s.suggestion.reason,
                other=OtherIssueOut(
                    id=s.other.id, title_en=s.other.title_en, version=s.other.version
                ),
            )
            for s in suggested
        ],
    )


@router.get("/options")
def list_options(user: CurrentUser, session: DbSession) -> OptionsOut:
    access.team_work(user)
    pack = partial(extraction.pack_for, user.org_id, session=session)
    found = inbox.options(session, pack, user.org_id)
    return OptionsOut(
        teams=[TeamOut(id=t.id, key=t.key, name=t.name) for t in found.teams],
        categories=[ChoiceOut(key=c.key, label=c.label) for c in found.categories],
        areas=found.areas,
        distributors=[NamedOut(id=i, name=n) for i, n in found.distributors],
        competitors=[NamedOut(id=i, name=n) for i, n in found.competitors],
        products=[NamedOut(id=i, name=n) for i, n in found.products],
        periods=[PeriodOut.model_validate(p, from_attributes=True) for p in found.periods],
    )


@router.get("/triage")
def list_triage(user: CurrentUser, session: DbSession) -> list[FindingOut]:
    """Confirmed insights with no team, for a manager or admin to give one."""
    access.only(user, *access.WHOLE_ORG)
    return [finding_out(item) for item in inbox.untriaged(session, user.org_id)]


@router.get("")
def list_issues(
    user: CurrentUser,
    session: DbSession,
    status: Annotated[list[Literal["open", "in_progress", "resolved"]] | None, Query()] = None,
    category: str | None = None,
    area: str | None = None,
    distributor: UUID | None = None,
    competitor: UUID | None = None,
    product: UUID | None = None,
    first_day: Annotated[date | None, Query(alias="from")] = None,
    last_day: Annotated[date | None, Query(alias="to")] = None,
    team: TeamKey = None,
) -> list[IssueOut]:
    """The team's issues: open and in progress unless `status` says otherwise; highest severity
    first, then most shops, then the most recently seen. `from` and `to` are IST days, both
    in the period. 400 unknown_competitor, unknown_product, period_backwards."""
    found = access.team(session, user, team)
    filters = inbox.Filters(
        statuses=tuple(status) if status else inbox.NO_FILTERS.statuses,
        category=category,
        area=area,
        distributor_id=str(distributor) if distributor else None,
        competitor_id=str(competitor) if competitor else None,
        sku_id=str(product) if product else None,
        first_day=first_day,
        last_day=last_day,
    )
    pack = partial(extraction.pack_for, user.org_id, session=session)
    return [issue_out(card) for card in inbox.cards(session, pack, user.org_id, found.id, filters)]


@router.get("/{issue_id}")
def get_issue(issue_id: UUID, user: CurrentUser, session: DbSession) -> IssueDetailOut:
    access.issue(session, user, str(issue_id))
    return detail_of(session, user.org_id, str(issue_id))


@router.patch("/{issue_id}")
def change_issue(
    issue_id: UUID, body: IssueChange, user: CurrentUser, session: DbSession
) -> IssueDetailOut:
    """409 issue_changed when the version read is out of date; nothing is written then."""
    access.issue(session, user, str(issue_id))
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
    return detail_of(session, user.org_id, str(issue_id))


@router.post("/suggestions/{suggestion_id}/merge")
def merge_suggested(
    suggestion_id: UUID, body: MergeIn, user: CurrentUser, session: DbSession
) -> IssueDetailOut:
    """Merge, as suggested: the issue kept, read again. Merged already: the same answer, nothing
    changed. 409 issue_changed (a version moved on), issue_merged, issue_closed, issue_empty,
    suggestion_dismissed; 404 suggestion_not_found."""
    access.suggestion(session, user, str(suggestion_id))
    pack = partial(extraction.pack_for, user.org_id, session=session)
    kept = merges.accept(
        session,
        pack,
        user.org_id,
        user.id,
        str(suggestion_id),
        (body.into_version, body.merge_version),
    )
    return detail_of(session, user.org_id, kept.id)


@router.post("/suggestions/{suggestion_id}/dismiss", status_code=204)
def dismiss_suggested(suggestion_id: UUID, user: CurrentUser, session: DbSession) -> None:
    """They stay apart, and the pair is never suggested again. 409 suggestion_merged."""
    access.suggestion(session, user, str(suggestion_id))
    merges.dismiss(session, user.org_id, user.id, str(suggestion_id))
