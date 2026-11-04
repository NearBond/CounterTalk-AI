"""Fixed since the last visit (batch 17b; the user, 3 Nov): what got fixed for a shop since
anyone last visited it, each with the team's resolution note, the line the rep says. No AI:
the note was written for the rep to pass on (batch 15). A rep's Updates page (batch 17e) lists
every shop's, newest fix first, from one query: one shop's list is the same query for it.

- **The shop's last visit** is its latest by anyone, before today (IST), that isn't failed or
  still recording. With none, nothing is listed: there's no "since".
- **Listed:** the issues holding a visible insight from this shop that are resolved now, with
  resolved_at after that visit. An issue reopened since, or open again, isn't. Each issue once;
  an insight routed to two teams is under each team's issue.
- **Each item:** the team, the category, the shop's own insight's summary (its latest, if the
  issue holds several of the shop's), the resolution note and when. Never a rep's name or a
  quote: another rep's visit may have raised it. Newest first, at most FIXED_MAX.

**The rep's own counts** (the user, 3 Nov), the line at the top of Updates: of the
problems they raised this IST month, how many are fixed. Only the rep's own: no rankings,
no comparison with other reps, no location (the plan's rule against tracking reps).
- Raised: visible insights of kind `problem` from the rep's own confirmed visits made
  this IST calendar month.
- Fixed: those routed to a team whose every routed issue is resolved now.

**Updates' list** (batch F7; the user, 5 Nov: the count said 3 fixed over a list of 2) lists
every fix the count counts too: each shop with a fix since its last visit, as above, and each
shop of the rep's own fixed problems this month. An issue fixed since the shop's last visit is
marked so; the rest were fixed before it. Each issue once per shop, newest first.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.orm import Session, aliased

from countertalk import rows
from countertalk.errors import NotFound
from countertalk.limits import FIXED_MAX
from countertalk.models import (
    ACTIVE_ISSUE,
    Insight,
    InsightRoute,
    Issue,
    IssueStatus,
    Shop,
    Team,
    Visit,
    VisitStatus,
    ist_date,
    utc_now,
)
from countertalk.summaries import at_midnight, period_of

# The playbook's kind for a problem the shopkeeper raised (fmcg-kirana-visit's kinds).
PROBLEM_KIND = "problem"


@dataclass(frozen=True)
class Fixed:
    team: str
    category: str
    problem: str
    note: str
    resolved_at: datetime
    # Resolved after the shop's last visit (Updates' "Fixed since the last visit here"), or
    # before it: one of the rep's own fixed problems this month, under plain "Fixed".
    since_last_visit: bool = True


@dataclass(frozen=True)
class ShopFixed:
    """One shop on Updates (batch 17e): its fixes, newest first."""

    shop_id: str
    name: str
    area: str
    items: list[Fixed]


def since_last_visit(session: Session, org_id: str, shop_id: str) -> list[Fixed]:
    shop = rows.find_one(
        session, Shop, org_id, shop_id, NotFound("shop_not_found", "There's no such shop")
    )
    return _fixed(session, org_id, shop.id).get(shop.id, [])


def for_rep(session: Session, org_id: str, rep_id: str) -> list[ShopFixed]:
    """Updates' list: each of the org's shops with something fixed since its last visit, and the
    shops of the rep's own fixed problems this month (my_counts' fixed) with theirs from before
    the shop's last visit. Each issue once per shop, newest first; the shop with the newest fix
    first."""
    found: dict[str, dict[str, Fixed]] = defaultdict(dict)
    for shop, items in _fixed_issues(session, org_id, None).items():
        found[shop].update(items)
    for shop, issue_id, item in _mine_fixed(session, org_id, rep_id):
        found[shop].setdefault(issue_id, item)
    if not found:
        return []
    shops = session.scalars(select(Shop).where(Shop.org_id == org_id, Shop.id.in_(found)))
    listed = [
        ShopFixed(
            shop.id,
            shop.name,
            shop.area,
            sorted(found[shop.id].values(), key=lambda item: item.resolved_at, reverse=True),
        )
        for shop in shops
    ]
    return sorted(listed, key=lambda each: (each.items[0].resolved_at, each.shop_id), reverse=True)


def _mine_fixed(session: Session, org_id: str, rep_id: str) -> list[tuple[str, str, Fixed]]:
    """The rep's own fixed problems this month, as my_counts counts them: (shop, issue, item)
    for each of their issues, the rep's latest insight in it. Each is marked by its own date:
    since the shop's last visit when resolved after it, or when the shop has no visit before
    today (_fixed_issues leaves those shops out, and cuts each list at FIXED_MAX)."""
    since = _last_visits(org_id)
    found = session.execute(
        select(*_FIXED_COLUMNS)
        .add_columns(or_(since.c.since.is_(None), Issue.resolved_at > since.c.since))
        .select_from(Insight)
        .join(Visit, Visit.id == Insight.visit_id)
        .join(InsightRoute, InsightRoute.insight_id == Insight.id)
        .join(Issue, Issue.id == InsightRoute.issue_id)
        .join(Team, (Team.org_id == Issue.org_id) & (Team.id == Issue.team_id))
        .outerjoin(since, since.c.shop_id == Visit.shop_id)
        .where(*_mine_this_month(org_id, rep_id), ~_open_route())
        .order_by(*_LATEST_FIRST)
    ).all()
    return [(shop, issue_id, item) for (shop, issue_id), item in _first_of_each(found).items()]


# A fix's columns, then whether it's since the shop's last visit: one row per insight routed to
# a resolved issue (_fixed_issues and _mine_fixed, R1).
_FIXED_COLUMNS = (
    Visit.shop_id,
    Issue.id,
    Team.name,
    Issue.category,
    Issue.resolution_note,
    Issue.resolved_at,
    Insight.summary_en,
)
# Per shop and issue, the shop's latest insight comes first: it's the one kept.
_LATEST_FIRST = (Visit.shop_id, Issue.id, Visit.created_at.desc(), Insight.seq.desc())


def _first_of_each(found: Any) -> dict[tuple[str, str], Fixed]:
    """Each (shop, issue)'s first row as a Fixed, in the rows' order: the latest insight."""
    kept: dict[tuple[str, str], Fixed] = {}
    for shop, issue_id, team, category, note, resolved_at, summary, since_last in found:
        if (shop, issue_id) not in kept and note is not None and resolved_at is not None:
            kept[shop, issue_id] = Fixed(team, category, summary, note, resolved_at, since_last)
    return kept


def _last_visits(org_id: str, shop_id: str | None = None) -> Any:
    """Each shop's last visit before today, the "since", as a subquery (shop_id, since)."""
    today = at_midnight(ist_date(utc_now()))
    last = select(Visit.shop_id, func.max(Visit.created_at).label("since")).where(
        Visit.org_id == org_id,
        Visit.status.not_in([VisitStatus.FAILED, VisitStatus.RECORDING]),
        Visit.created_at < today,
    )
    if shop_id is not None:
        last = last.where(Visit.shop_id == shop_id)
    return last.group_by(Visit.shop_id).subquery()


def _fixed(session: Session, org_id: str, shop_id: str | None) -> dict[str, list[Fixed]]:
    """The module's rules, for one shop or (None) every shop of the org, in one query: each
    shop with a fix, its list newest first, at most FIXED_MAX."""
    found = _fixed_issues(session, org_id, shop_id)
    return {shop: list(items.values()) for shop, items in found.items()}


def _fixed_issues(
    session: Session, org_id: str, shop_id: str | None
) -> dict[str, dict[str, Fixed]]:
    """_fixed's lists, each fix by its issue's ID (dicts keep their order: newest first)."""
    since = _last_visits(org_id, shop_id)
    found = session.execute(
        select(*_FIXED_COLUMNS)
        .add_columns(true())
        .select_from(Issue)
        .join(Team, (Team.org_id == Issue.org_id) & (Team.id == Issue.team_id))
        .join(InsightRoute, InsightRoute.issue_id == Issue.id)
        .join(Insight, Insight.id == InsightRoute.insight_id)
        .join(Visit, Visit.id == Insight.visit_id)
        .join(since, since.c.shop_id == Visit.shop_id)
        .where(
            Issue.org_id == org_id,
            Issue.status == IssueStatus.RESOLVED,
            Issue.resolved_at > since.c.since,
            Insight.visible,
        )
        .order_by(*_LATEST_FIRST)
    ).all()
    by_shop: dict[str, dict[str, Fixed]] = defaultdict(dict)
    for (shop, issue_id), item in sorted(
        _first_of_each(found).items(), key=lambda each: each[1].resolved_at, reverse=True
    ):
        if len(by_shop[shop]) < FIXED_MAX:
            by_shop[shop][issue_id] = item  # newest first
    return dict(by_shop)


@dataclass(frozen=True)
class Home:
    """The rep's home's counts (batch F2): their own visits waiting for review, and the shops
    with something fixed since their last visit: news to pass on, Updates' "Fixed since the last
    visit here". Its plain "Fixed" (batch F7, the rep's own from before the last visit) isn't
    news, so it isn't counted here."""

    to_review: int
    to_pass_on: int


def home(session: Session, org_id: str, rep_id: str) -> Home:
    waiting = select(func.count()).where(
        Visit.org_id == org_id, Visit.rep_id == rep_id, Visit.status == VisitStatus.REVIEW
    )
    return Home(session.scalar(waiting) or 0, len(_fixed(session, org_id, None)))


@dataclass(frozen=True)
class Counts:
    raised: int
    fixed: int


def _problems_of(org_id: str, rep_id: str) -> list[Any]:
    """The rep's own problems: visible insights of kind problem from their confirmed visits
    (the counts' and the pending list's one rule, R1)."""
    return _problems(org_id, Visit.rep_id == rep_id)


def _problems(org_id: str, whose: Any) -> list[Any]:
    """Visible insights of kind problem from confirmed visits: a rep's (`whose` names them) or a
    shop's."""
    return [
        Insight.org_id == org_id,
        Insight.kind == PROBLEM_KIND,
        Insight.visible,
        whose,
        Visit.status == VisitStatus.CONFIRMED,
    ]


def _mine_this_month(org_id: str, rep_id: str) -> list[Any]:
    """The rep's own problems this IST month: the counts' raised, and Updates' list (R1)."""
    month = period_of("month", ist_date(utc_now()))
    return [
        *_problems_of(org_id, rep_id),
        Visit.created_at >= month.start,
        Visit.created_at < month.end,
    ]


def _open_route() -> Any:
    """The insight has a route whose issue isn't resolved now: it isn't fixed (R1). Its own
    aliases, so it correlates with the insight alone where the query joins routes and issues."""
    route, issue = aliased(InsightRoute), aliased(Issue)
    return exists().where(
        route.insight_id == Insight.id,
        ~exists().where(and_(issue.id == route.issue_id, issue.status == IssueStatus.RESOLVED)),
    )


def my_counts(session: Session, org_id: str, rep_id: str) -> Counts:
    """The rep's own problems this IST month, and how many are fixed now."""
    mine = (
        select(Insight.id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(*_mine_this_month(org_id, rep_id))
    )
    routed = exists().where(InsightRoute.insight_id == Insight.id)
    raised = session.scalar(select(func.count()).select_from(mine.subquery())) or 0
    done = mine.where(routed, ~_open_route())
    fixed = session.scalar(select(func.count()).select_from(done.subquery())) or 0
    return Counts(raised, fixed)


@dataclass(frozen=True)
class Pending:
    """One of the rep's problems still being worked on (batch F3): its issue in one team's inbox."""

    issue_id: str
    shop: str
    area: str
    team: str
    # The rep's own insight's summary, as it said it.
    problem: str
    status: str
    raised_at: datetime


def pending(session: Session, org_id: str, rep_id: str) -> list[Pending]:
    """The rep's own problems whose issue is open or in progress now: one row per issue (a team's),
    the rep's latest insight in it, newest first. Never another rep's name, never a quote."""
    return _still_open(session, _problems_of(org_id, rep_id))


def shop_pending(session: Session, org_id: str, shop_id: str) -> list[Pending]:
    """The shop's problems still open, raised on any rep's visit (batch F5's brief), as the rep's
    pending list words them."""
    return _still_open(session, _problems(org_id, Visit.shop_id == shop_id))


def _still_open(session: Session, problems: list[Any]) -> list[Pending]:
    found = session.execute(
        select(Issue.id, Shop.name, Shop.area, Team.name)
        .add_columns(Insight.summary_en, Issue.status, Visit.created_at)
        .select_from(Insight)
        .join(Visit, Visit.id == Insight.visit_id)
        .join(Shop, Shop.id == Visit.shop_id)
        .join(InsightRoute, InsightRoute.insight_id == Insight.id)
        .join(Issue, Issue.id == InsightRoute.issue_id)
        .join(Team, (Team.org_id == Issue.org_id) & (Team.id == Issue.team_id))
        .where(*problems, Issue.status.in_(ACTIVE_ISSUE))
        .order_by(Visit.created_at.desc(), Insight.seq.desc())
    ).all()
    kept: dict[str, Pending] = {}
    for issue_id, shop, area, team, problem, status, raised_at in found:
        kept.setdefault(issue_id, Pending(issue_id, shop, area, team, problem, status, raised_at))
    return list(kept.values())
