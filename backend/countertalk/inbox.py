"""The inbox's reads (batch 15): a team's issues as cards, one issue with its insights, and the
triage list. Read-only: the changes are issues.change and routing.py's.

Every count is of an issue's routes (issues.with_routes: an issue with none is never shown or
counted), in what it says it counts: shops, insights and reps, never mentions (R10). Raised
again is its own flag: a shop with insights from two or more visits in the issue. Until batch
15b's score, the order is severity (highest first), then shops, then the most recently seen.

**The filters** pick issues and never change their numbers: a card's counts are its whole
issue's. Status and category are the issue's own. The rest (area, distributor, and batch
17c's competitor, product and period) are its insights': an issue is kept when one of its
insights meets every one of them, so "this competitor, this month" means that competitor's
insights from this month. A competitor or product that isn't the org's catalog item of that
kind, and a period that ends before it starts, are 400, never an empty list. A brand takes in
its products (batch 17d; the planner, 3 Nov): "Lay's" is Lay's and every Lay's product of
the same kind and company; a product is itself alone.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, get_args

from sqlalchemy import Select, case, distinct, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from countertalk import entities, issues, limits, review, routing, rows, summaries
from countertalk.errors import Invalid
from countertalk.models import (
    ACTIVE_ISSUE,
    CatalogItem,
    CatalogKind,
    Distributor,
    Insight,
    InsightRoute,
    Issue,
    Shop,
    Team,
    User,
    Visit,
    ist_date,
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
    # Batch 17c: a competitor's catalog item, one of the org's own products, and a period of
    # IST days, both ends in it (either may be left open).
    competitor_id: str | None = None
    sku_id: str | None = None
    first_day: date | None = None
    last_day: date | None = None


DAY = timedelta(days=1)

# Open and in progress, of any category, area, distributor, competitor, product and time.
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
class Finding:
    """A confirmed insight with who, where and when: the triage view's, with no team, and an
    answer's (batch 17d)."""

    insight: review.InsightView
    shop_name: str
    area: str
    rep_name: str
    visit_id: str
    visited_at: datetime


def team_of(session: Session, org_id: str, key: str) -> Team:
    """A team of the org, by its key (a manager's pick; access.team says whose)."""
    team = session.scalars(select(Team).where(Team.org_id == org_id, Team.key == key)).first()
    if team is None:
        raise routing.missing_team()
    return team


@dataclass(frozen=True)
class PeriodChoice:
    """The period picker's ready-made periods, as the IST days the filter takes."""

    key: str
    label: str
    first_day: date
    last_day: date


@dataclass(frozen=True)
class Options:
    """What the inbox's pickers offer: the teams (a manager's picker, Move and triage), and the
    filters' categories, areas, distributors, competitors and products (ID and name), and
    periods. Only the competitors and products some issue's insight links to: the
    dropdowns aren't the whole catalog."""

    teams: list[Team]
    categories: list[review.Choice]
    areas: list[str]
    distributors: list[tuple[str, str]]
    competitors: list[tuple[str, str]]
    products: list[tuple[str, str]]
    periods: list[PeriodChoice]


def periods(today: date) -> list[PeriodChoice]:
    """This and last week (Monday to Sunday), and this and last month, as summaries count
    them (R1)."""
    found = []
    for kind in get_args(summaries.PeriodKind):
        this = summaries.period_of(kind, today)
        for when, period in (("this", this), ("last", summaries.previous(this))):
            last_day = ist_date(period.end) - DAY
            label = f"{when.capitalize()} {kind}"
            found.append(PeriodChoice(f"{when}_{kind}", label, ist_date(period.start), last_day))
    return found


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
        competitors=_linked(session, org_id, CatalogKind.COMPETITOR, Insight.competitor_id),
        products=_linked(session, org_id, CatalogKind.OWN, Insight.sku_id),
        periods=periods(ist_date(utc_now())),
    )


def _linked(session: Session, org_id: str, kind: str, link: Any) -> list[tuple[str, str]]:
    """The org's catalog items of this kind that an insight in some issue links to, by name:
    a brand, too, when only its products are linked."""
    in_an_issue = exists().where(
        link.in_(_family(CatalogItem).correlate(CatalogItem)),
        InsightRoute.insight_id == Insight.id,
        InsightRoute.issue_id.is_not(None),  # an unconfirmed visit's route has none yet
    )
    found = session.execute(
        select(CatalogItem.id, CatalogItem.brand, CatalogItem.product).where(
            CatalogItem.org_id == org_id, CatalogItem.kind == kind, in_an_issue
        )
    )
    named = [(item_id, entities.item_name(brand, product)) for item_id, brand, product in found]
    return sorted(named, key=lambda each: (each[1].casefold(), each[0]))


def check(session: Session, org_id: str, filters: Filters) -> None:
    """400 for a competitor or product that isn't the org's catalog item of that kind (another
    org's, or a product given as a competitor), and for a period that ends before it starts or
    has a day outside the years it takes."""
    wanted = (
        (filters.competitor_id, CatalogKind.COMPETITOR, "competitor", "one of the competitors"),
        (filters.sku_id, CatalogKind.OWN, "product", "one of the org's own products"),
    )
    for item_id, kind, name, what in wanted:
        if item_id is None:
            continue
        refused = Invalid(f"unknown_{name}", f"That isn't {what} in the catalog")
        if rows.find_one(session, CatalogItem, org_id, item_id, refused).kind != kind:
            raise refused
    first, last = filters.first_day, filters.last_day
    for day in (first, last):
        if day is not None and not limits.PERIOD_FIRST_YEAR <= day.year <= limits.PERIOD_LAST_YEAR:
            raise Invalid(
                "period_out_of_range",
                f"A period's days are from {limits.PERIOD_FIRST_YEAR} to {limits.PERIOD_LAST_YEAR}",
            )
    if first is not None and last is not None and first > last:
        raise Invalid("period_backwards", "The period ends before it starts")


def cards(
    session: Session,
    pack_for: Callable[[], OrgPack],
    org_id: str,
    team_id: str,
    filters: Filters = NO_FILTERS,
) -> list[Card]:
    """The team's issues with routes, filtered, highest severity first, then most shops, then
    the most recently seen. 400 for a filter check() refuses."""
    check(session, org_id, filters)
    counts = _counts(Issue.org_id == org_id, Issue.team_id == team_id).subquery()
    query = (
        _cards_select(counts)
        .where(Issue.org_id == org_id, Issue.team_id == team_id)
        .where(Issue.status.in_(filters.statuses))
    )
    if filters.category is not None:
        query = query.where(Issue.category == filters.category)
    member = _member_conditions(filters)
    if member:
        query = query.where(_has_member(*member))
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


def in_triage(org_id: str) -> tuple[Any, ...]:
    """The triage list's insights: the org's confirmed (visible) ones with no route, so no team.
    The list's and the overview's count's one rule (R1)."""
    no_route = ~exists().where(InsightRoute.insight_id == Insight.id)
    return (Insight.org_id == org_id, Insight.visible.is_(True), no_route)


def untriaged(session: Session, org_id: str) -> list[Finding]:
    """The org's confirmed insights with no team, most recently seen first."""
    found = session.execute(
        _who(select(Insight))
        .where(*in_triage(org_id))
        .order_by(Visit.created_at.desc(), Insight.seq)
    ).all()
    return _as_findings(session, found)


def findings(
    session: Session, org_id: str, filters: Filters, team_id: str | None, limit: int
) -> list[Finding]:
    """Batch 17d's answer: the visible insights in an issue of the filters' statuses and
    category (a team member's: in their team's issues) that meet every insight filter, newest
    first, at most `limit`. 400 for a filter check() refuses."""
    check(session, org_id, filters)
    in_an_issue = [
        InsightRoute.insight_id == Insight.id,
        Issue.id == InsightRoute.issue_id,
        Issue.status.in_(filters.statuses),
    ]
    if filters.category is not None:
        in_an_issue.append(Issue.category == filters.category)
    if team_id is not None:
        in_an_issue.append(InsightRoute.team_id == team_id)
    found = session.execute(
        _who(select(Insight))
        .where(Insight.org_id == org_id, Insight.visible.is_(True))
        .where(exists().where(*in_an_issue), *_member_conditions(filters))
        .order_by(Visit.created_at.desc(), Insight.seq)
        .limit(limit)
    ).all()
    return _as_findings(session, found)


def _as_findings(session: Session, found: Any) -> list[Finding]:
    """_who's rows as findings, each with its insight's view (quotes and routes)."""
    views = {
        view.insight.id: view for view in review.insight_views(session, [row[0] for row in found])
    }
    return [
        Finding(views[insight.id], shop_name, area, rep_name, visit_id, visited_at)
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


def _member_conditions(filters: Filters) -> list[Any]:
    """The filters on an issue's insights: on the insight, its visit or its shop."""
    found = []
    if filters.area is not None:
        found.append(Shop.area == filters.area)
    if filters.distributor_id is not None:
        found.append(Shop.distributor_id == filters.distributor_id)
    if filters.competitor_id is not None:
        found.append(_takes_in(Insight.competitor_id, filters.competitor_id))
    if filters.sku_id is not None:
        found.append(_takes_in(Insight.sku_id, filters.sku_id))
    if filters.first_day is not None:
        found.append(Visit.created_at >= summaries.at_midnight(filters.first_day))
    if filters.last_day is not None:
        found.append(Visit.created_at < summaries.at_midnight(filters.last_day + DAY))
    return found


def _family(picked: Any) -> Select:
    """The catalog items a pick takes in: a brand's own and its products' (the same kind,
    company and brand), or a product alone."""
    member = aliased(CatalogItem)
    return select(member.id).where(
        *entities.same_brand(member, picked),
        or_(picked.product.is_(None), member.id == picked.id),
    )


def _takes_in(link: Any, item_id: str) -> Any:
    """The insight links to the picked item, or to one it takes in."""
    picked = aliased(CatalogItem)
    return link.in_(_family(picked).where(picked.id == item_id))


def _has_member(*conditions: Any) -> Any:
    """One insight of the issue meets every condition (on it, its visit or its shop)."""
    return exists().where(
        InsightRoute.issue_id == Issue.id,
        Insight.id == InsightRoute.insight_id,
        Visit.id == Insight.visit_id,
        Shop.id == Visit.shop_id,
        *conditions,
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
