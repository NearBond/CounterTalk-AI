"""The inbox's reads (batch 15): a team's issues as cards, one issue with its insights, and the
triage list. Read-only: the changes are issues.change and routing.py's.

Every count is of an issue's routes (issues.with_routes: an issue with none is never shown or
counted), in what it says it counts: shops, insights and reps, never mentions (R10). Raised
again is its own flag: a shop with insights from two or more visits in the issue. Until batch
15b's score, the order is severity (highest first), then shops, then the most recently seen.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, distinct, exists, func, select
from sqlalchemy.orm import Session

from countertalk import issues, review
from countertalk.errors import NotFound
from countertalk.models import (
    ACTIVE_ISSUE,
    Distributor,
    Insight,
    InsightRoute,
    Issue,
    Shop,
    Team,
    User,
    Visit,
    parse_id,
    utc_now,
)
from countertalk.playbooks import OrgPack


@dataclass(frozen=True)
class Filters:
    statuses: tuple[str, ...] = tuple(status.value for status in ACTIVE_ISSUE)
    category: str | None = None
    area: str | None = None
    distributor_id: str | None = None


# Open and in progress, of any category, area and distributor.
NO_FILTERS = Filters()


@dataclass(frozen=True)
class Card:
    issue: Issue
    team_key: str
    owner_name: str | None
    shops: int
    insights: int
    reps: int
    raised_again: bool
    overdue: bool


@dataclass(frozen=True)
class Member:
    """One insight of an issue, as its card shows it: who, where and when, and its route here."""

    insight: review.InsightView
    shop_name: str
    area: str
    rep_name: str
    visit_id: str
    visited_at: datetime
    route: review.RouteView


@dataclass(frozen=True)
class Detail:
    card: Card
    members: list[Member]


@dataclass(frozen=True)
class Untriaged:
    """A confirmed insight with no team, as the triage view shows it."""

    insight: review.InsightView
    shop_name: str
    area: str
    rep_name: str
    visit_id: str
    visited_at: datetime


def team_of(session: Session, org_id: str, key: str) -> Team:
    """The inbox's team, by its key: the dev dropdown's until batch 16 signs a member in."""
    team = session.scalars(select(Team).where(Team.org_id == org_id, Team.key == key)).first()
    if team is None:
        raise NotFound("team_not_found", "There's no such team")
    return team


@dataclass(frozen=True)
class Options:
    """What the inbox's pickers offer: the teams (the dev dropdown, Move and triage), and the
    filters' categories, areas and distributors (ID and name)."""

    teams: list[Team]
    categories: list[review.Choice]
    areas: list[str]
    distributors: list[tuple[str, str]]


def options(session: Session, pack_for: Callable[[], OrgPack], org_id: str) -> Options:
    areas = select(Shop.area).where(Shop.org_id == org_id).distinct().order_by(Shop.area)
    distributors = (
        select(Distributor.id, Distributor.name)
        .where(Distributor.org_id == org_id)
        .order_by(Distributor.name)
    )
    return Options(
        teams=list(session.scalars(select(Team).where(Team.org_id == org_id).order_by(Team.name))),
        categories=review.category_choices(pack_for()),
        areas=list(session.scalars(areas)),
        distributors=[(found_id, name) for found_id, name in session.execute(distributors)],
    )


def cards(
    session: Session,
    pack_for: Callable[[], OrgPack],
    org_id: str,
    team_id: str,
    filters: Filters = NO_FILTERS,
) -> list[Card]:
    """The team's issues with routes, filtered, highest severity first, then most shops, then
    the most recently seen."""
    counts = _counts(Issue.org_id == org_id, Issue.team_id == team_id).subquery()
    query = (
        _cards_select(counts)
        .where(Issue.org_id == org_id, Issue.team_id == team_id)
        .where(Issue.status.in_(filters.statuses))
    )
    if filters.category is not None:
        query = query.where(Issue.category == filters.category)
    if filters.area is not None:
        query = query.where(_has_shop(Shop.area == filters.area))
    if filters.distributor_id is not None:
        query = query.where(_has_shop(Shop.distributor_id == filters.distributor_id))
    pack = pack_for()
    ranks = {key: issues.severity_rank(pack, key) for key in pack.playbook.severities}
    rank = case(ranks, value=Issue.severity, else_=len(ranks))
    query = query.order_by(rank, counts.c.shops.desc(), Issue.last_seen_at.desc(), Issue.seq.desc())
    return [_card(row) for row in session.execute(query)]


def detail(session: Session, org_id: str, issue_id: str) -> Detail:
    """One issue, its card and each of its insights, oldest visit first. Another org's, or one
    with no routes, is 404."""
    found = parse_id(issue_id)
    mine = (Issue.id == found, Issue.org_id == org_id)
    counts = _counts(*mine).subquery()
    row = None
    if found is not None:
        row = session.execute(_cards_select(counts).where(*mine)).one_or_none()
    if row is None:
        raise issues.missing_issue()
    members = session.execute(
        _who(select(Insight).join(InsightRoute, InsightRoute.insight_id == Insight.id))
        .where(InsightRoute.issue_id == found)
        .order_by(Visit.created_at, Insight.seq)
    ).all()
    views = {
        view.insight.id: view
        for view in review.insight_views(session, [member[0] for member in members])
    }
    shown = []
    for found_insight, shop_name, area, rep_name, visit_id, visited_at in members:
        view = views[found_insight.id]
        # Its route in this issue; one moved out meanwhile (a write's later read) is left out.
        route = next((each for each in view.routes if each.issue_id == found), None)
        if route is not None:
            shown.append(Member(view, shop_name, area, rep_name, visit_id, visited_at, route))
    return Detail(_card(row), shown)


def untriaged(session: Session, org_id: str) -> list[Untriaged]:
    """The org's confirmed insights with no team, most recently seen first."""
    no_route = ~exists().where(InsightRoute.insight_id == Insight.id)
    found = session.execute(
        _who(select(Insight))
        .where(Insight.org_id == org_id, Insight.visible.is_(True), no_route)
        .order_by(Visit.created_at.desc(), Insight.seq)
    ).all()
    views = {
        view.insight.id: view for view in review.insight_views(session, [row[0] for row in found])
    }
    return [
        Untriaged(views[insight.id], shop_name, area, rep_name, visit_id, visited_at)
        for insight, shop_name, area, rep_name, visit_id, visited_at in found
    ]


def _who(query: Select) -> Select:
    """An insight query with its visit's shop, rep and time."""
    return (
        query.join(Visit, Visit.id == Insight.visit_id)
        .join(Shop, Shop.id == Visit.shop_id)
        .join(User, User.id == Visit.rep_id)
        .add_columns(Shop.name, Shop.area, User.name, Visit.id, Visit.created_at)
    )


def _counts(*scope: Any) -> Select:
    """Per issue in scope (conditions on Issue): its shops, insights and reps, and how many of
    its shops raised it on two or more visits."""
    per_shop = (
        select(
            InsightRoute.issue_id,
            Visit.shop_id,
            func.count(distinct(Visit.id)).label("visits"),
        )
        .join(Issue, Issue.id == InsightRoute.issue_id)
        .join(Insight, Insight.id == InsightRoute.insight_id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(*scope)
        .group_by(InsightRoute.issue_id, Visit.shop_id)
        .subquery()
    )
    totals = (
        select(
            InsightRoute.issue_id,
            func.count(distinct(Visit.shop_id)).label("shops"),
            func.count(distinct(Insight.id)).label("insights"),
            func.count(distinct(Visit.rep_id)).label("reps"),
        )
        .join(Issue, Issue.id == InsightRoute.issue_id)
        .join(Insight, Insight.id == InsightRoute.insight_id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(*scope)
        .group_by(InsightRoute.issue_id)
        .subquery()
    )
    again = (
        select(per_shop.c.issue_id, func.count().label("shops_again"))
        .where(per_shop.c.visits >= 2)
        .group_by(per_shop.c.issue_id)
        .subquery()
    )
    return select(
        totals.c.issue_id,
        totals.c.shops,
        totals.c.insights,
        totals.c.reps,
        func.coalesce(again.c.shops_again, 0).label("shops_again"),
    ).outerjoin(again, again.c.issue_id == totals.c.issue_id)


def _cards_select(counts: Any) -> Select:
    """Each issue with routes: itself, its team's key, its owner's name and its counts.
    issues.with_routes() alone decides which issues those are (R1, R10)."""
    owner = User.__table__.alias("owner")
    counted = [
        func.coalesce(counts.c[name], 0) for name in ("shops", "insights", "reps", "shops_again")
    ]
    return (
        select(Issue, Team.key, owner.c.name)
        .add_columns(*counted)
        .join(Team, Team.id == Issue.team_id)
        .outerjoin(counts, counts.c.issue_id == Issue.id)
        .outerjoin(owner, owner.c.id == Issue.owner_id)
        .where(issues.with_routes())
    )


def _has_shop(condition: Any) -> Any:
    """Some insight of the issue is from a shop that meets the condition."""
    return exists().where(
        InsightRoute.issue_id == Issue.id,
        Insight.id == InsightRoute.insight_id,
        Visit.id == Insight.visit_id,
        Shop.id == Visit.shop_id,
        condition,
    )


def _card(row: Any) -> Card:
    issue, team_key, owner_name, shops, insights, reps, shops_again = row
    return Card(
        issue=issue,
        team_key=team_key,
        owner_name=owner_name,
        shops=shops,
        insights=insights,
        reps=reps,
        raised_again=shops_again > 0,
        overdue=issue.status in ACTIVE_ISSUE and issue.due_at < utc_now(),
    )
