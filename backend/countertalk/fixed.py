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
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

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


def every_shop(session: Session, org_id: str) -> list[ShopFixed]:
    """Each of the org's shops with something fixed since its last visit, its newest fix first."""
    found = _fixed(session, org_id, None)
    if not found:
        return []
    shops = session.scalars(select(Shop).where(Shop.org_id == org_id, Shop.id.in_(found)))
    listed = [ShopFixed(shop.id, shop.name, shop.area, found[shop.id]) for shop in shops]
    return sorted(listed, key=lambda each: (each.items[0].resolved_at, each.shop_id), reverse=True)


def _fixed(session: Session, org_id: str, shop_id: str | None) -> dict[str, list[Fixed]]:
    """The module's rules, for one shop or (None) every shop of the org, in one query: each
    shop with a fix, its list newest first, at most FIXED_MAX."""
    today = at_midnight(ist_date(utc_now()))
    # Each shop's last visit before today: the "since".
    last = select(Visit.shop_id, func.max(Visit.created_at).label("since")).where(
        Visit.org_id == org_id,
        Visit.status.not_in([VisitStatus.FAILED, VisitStatus.RECORDING]),
        Visit.created_at < today,
    )
    if shop_id is not None:
        last = last.where(Visit.shop_id == shop_id)
    since = last.group_by(Visit.shop_id).subquery()
    found = session.execute(
        select(Visit.shop_id, Issue.id, Team.name, Issue.category)
        .add_columns(Issue.resolution_note, Issue.resolved_at, Insight.summary_en)
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
        # Per shop and issue, the shop's latest insight comes first: it's the one kept.
        .order_by(Visit.shop_id, Issue.id, Visit.created_at.desc(), Insight.seq.desc())
    ).all()
    kept: dict[tuple[str, str], Fixed] = {}
    for shop, issue_id, team, category, note, resolved_at, summary in found:
        if (shop, issue_id) not in kept and note is not None and resolved_at is not None:
            kept[shop, issue_id] = Fixed(team, category, summary, note, resolved_at)
    by_shop: dict[str, list[Fixed]] = defaultdict(list)
    for (shop, _), item in sorted(kept.items(), key=lambda each: each[1].resolved_at, reverse=True):
        by_shop[shop].append(item)  # newest first
    return {shop: items[:FIXED_MAX] for shop, items in by_shop.items()}


@dataclass(frozen=True)
class Home:
    """The rep's home's counts (batch F2): their own visits waiting for review, and the shops
    with something fixed since their last visit, as Updates lists them."""

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


def my_counts(session: Session, org_id: str, rep_id: str) -> Counts:
    """The rep's own problems this IST month, and how many are fixed now."""
    month = period_of("month", ist_date(utc_now()))
    mine = (
        select(Insight.id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(
            *_problems_of(org_id, rep_id),
            Visit.created_at >= month.start,
            Visit.created_at < month.end,
        )
    )
    routed = exists().where(InsightRoute.insight_id == Insight.id)
    open_route = exists().where(
        InsightRoute.insight_id == Insight.id,
        ~exists().where(
            and_(Issue.id == InsightRoute.issue_id, Issue.status == IssueStatus.RESOLVED)
        ),
    )
    raised = session.scalar(select(func.count()).select_from(mine.subquery())) or 0
    done = mine.where(routed, ~open_route)
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
