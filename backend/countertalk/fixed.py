"""Fixed since the last visit (batch 17b; the user, 3 Nov): when a rep picks a shop, what got
fixed for it since anyone last visited, each with the team's resolution note, the line the rep
says. No AI: the note was written for the rep to pass on (batch 15).

- **The shop's last visit** is its latest by anyone, before today (IST), that isn't failed or
  still recording. With none, nothing is listed: there's no "since".
- **Listed:** the issues holding a visible insight from this shop that are resolved now, with
  resolved_at after that visit. An issue reopened since, or open again, isn't. Each issue once;
  an insight routed to two teams is under each team's issue.
- **Each item:** the team, the category, the shop's own insight's summary (its latest, if the
  issue holds several of the shop's), the resolution note and when. Never a rep's name or a
  quote: another rep's visit may have raised it. Newest first, at most FIXED_MAX.

**The rep's own counts** (the user, 3 Nov), the line at the top of the shop pick: of the
problems they raised this IST month, how many are fixed. Only the rep's own: no rankings,
no comparison with other reps, no location (the plan's rule against tracking reps).
- Raised: visible insights of kind `problem` from the rep's own confirmed visits made
  this IST calendar month.
- Fixed: those routed to a team whose every routed issue is resolved now.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from countertalk import rows
from countertalk.errors import NotFound
from countertalk.limits import FIXED_MAX
from countertalk.models import (
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


def last_visit_before_today(session: Session, shop: Shop) -> datetime | None:
    today = at_midnight(ist_date(utc_now()))
    return session.scalar(
        select(func.max(Visit.created_at)).where(
            Visit.org_id == shop.org_id,
            Visit.shop_id == shop.id,
            Visit.status.not_in([VisitStatus.FAILED, VisitStatus.RECORDING]),
            Visit.created_at < today,
        )
    )


def since_last_visit(session: Session, org_id: str, shop_id: str) -> list[Fixed]:
    shop = rows.find_one(
        session, Shop, org_id, shop_id, NotFound("shop_not_found", "There's no such shop")
    )
    since = last_visit_before_today(session, shop)
    if since is None:
        return []
    found = session.execute(
        select(
            Issue.id,
            Team.name,
            Issue.category,
            Issue.resolution_note,
            Issue.resolved_at,
            Insight.summary_en,
        )
        .join(Team, (Team.org_id == Issue.org_id) & (Team.id == Issue.team_id))
        .join(InsightRoute, InsightRoute.issue_id == Issue.id)
        .join(Insight, Insight.id == InsightRoute.insight_id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(
            Issue.org_id == org_id,
            Issue.status == IssueStatus.RESOLVED,
            Issue.resolved_at > since,
            Visit.shop_id == shop.id,
            Insight.visible,
        )
        # Per issue, the shop's latest insight comes first: it's the one kept.
        .order_by(Issue.id, Visit.created_at.desc(), Insight.seq.desc())
    ).all()
    kept: dict[str, Fixed] = {}
    for issue_id, team, category, note, resolved_at, summary in found:
        if issue_id not in kept and note is not None and resolved_at is not None:
            kept[issue_id] = Fixed(team, category, summary, note, resolved_at)
    return sorted(kept.values(), key=lambda item: item.resolved_at, reverse=True)[:FIXED_MAX]


@dataclass(frozen=True)
class Counts:
    raised: int
    fixed: int


def my_counts(session: Session, org_id: str, rep_id: str) -> Counts:
    """The rep's own problems this IST month, and how many are fixed now."""
    month = period_of("month", ist_date(utc_now()))
    mine = (
        select(Insight.id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(
            Insight.org_id == org_id,
            Insight.kind == PROBLEM_KIND,
            Insight.visible,
            Visit.rep_id == rep_id,
            Visit.status == VisitStatus.CONFIRMED,
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
