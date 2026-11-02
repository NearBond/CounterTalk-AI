"""A team summary's drill-down (batch 15c; BRIEF D16): the summary → an issue → its reps → their
shops and visits → each quote, and ▶. From SQL, with no AI words.

It lists the issue's latest run up to the period's end and anything else from the period, as
the summary counts them (summaries.run_of), so its counts are the summary's: the run's shops
are its open shops while it's open, and the period's shops, insights, reps and visits, and
the shops raising it again, are the summary's own. Counted now
(R10): a summary written before a later change, a merge say, keeps the numbers it had.

Reps are listed by name, each with their shops and how many insights: never ranked or compared.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk import review, summaries
from countertalk.models import Distributor, Insight, Issue, Shop, User, utc_now
from countertalk.summaries import Period, Totals


@dataclass(frozen=True)
class Visited:
    visit_id: str
    visited_at: datetime
    in_period: bool
    insights: list[review.InsightView]


@dataclass(frozen=True)
class ShopVisits:
    shop_id: str
    name: str
    area: str
    distributor: str | None
    visits: list[Visited]


@dataclass(frozen=True)
class RepShops:
    rep_id: str
    rep_name: str
    insights: int
    shops: list[ShopVisits]


@dataclass(frozen=True)
class Drill:
    issue: Issue
    # As it was at the period's end.
    status: str
    period: Period
    as_of: datetime
    # Its latest run, up to the period's end: shops (its open shops while it's open), insights
    # and reps. Raised again: shops with two or more visits up to the period's end, as the
    # summary counts it.
    shops: int
    insights: int
    reps: int
    raised_again: int
    # The period's: the summary's own shops, insights, reps and visits.
    in_period: Totals
    by_rep: list[RepShops]


def drill(
    session: Session,
    org_id: str,
    team_id: str,
    issue_id: str,
    period: Period,
    now: datetime | None = None,
) -> Drill:
    """The team's issue, drilled down for the period (the module docstring); 404 if it isn't one
    of the team's issues the summary has."""
    run = summaries.run_of(session, org_id, team_id, issue_id, period)
    in_period = [m for m in run.members if period.holds(m.seen_at)]
    # Listed: the latest run, and anything else from the period (a resolve and a reopen
    # within it), so its visits are the summary's and the run's both.
    members = {m.insight_id: m for m in [*run.run, *in_period]}
    reps = select(User.id, User.name).where(User.id.in_({m.rep_id for m in members.values()}))
    names = {rep_id: name for rep_id, name in session.execute(reps)}
    shops = {
        shop.id: (shop, distributor)
        for shop, distributor in session.execute(
            select(Shop, Distributor.name)
            .outerjoin(Distributor, Distributor.id == Shop.distributor_id)
            .where(Shop.id.in_({m.shop_id for m in members.values()}))
        )
    }
    found = session.scalars(select(Insight).where(Insight.id.in_(list(members)))).all()
    views = review.insight_views(session, list(found))
    grouped: dict[str, dict[str, dict[str, list[review.InsightView]]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(list))
    )
    for view in views:
        m = members[view.insight.id]
        grouped[m.rep_id][m.shop_id][m.visit_id].append(view)
    seen = {m.visit_id: m.seen_at for m in members.values()}
    by_rep = []
    for rep_id, by_shop in grouped.items():
        shop_rows = []
        for shop_id, by_visit in by_shop.items():
            shop, distributor = shops[shop_id]
            visits = [
                Visited(visit_id, seen[visit_id], period.holds(seen[visit_id]), insights)
                for visit_id, insights in sorted(by_visit.items(), key=lambda v: seen[v[0]])
            ]
            shop_rows.append(ShopVisits(shop_id, shop.name, shop.area, distributor, visits))
        count = sum(len(v.insights) for s in shop_rows for v in s.visits)
        shop_rows.sort(key=lambda s: (s.name, s.shop_id))
        by_rep.append(RepShops(rep_id, names[rep_id], count, shop_rows))
    by_rep.sort(key=lambda r: (r.rep_name, r.rep_id))
    return Drill(
        issue=run.issue,
        status=run.status,
        period=period,
        as_of=now or utc_now(),
        shops=len({m.shop_id for m in run.run}),
        insights=len(run.run),
        reps=len({m.rep_id for m in run.run}),
        raised_again=summaries.raised_again_of(run.members),
        in_period=summaries.totals_of(in_period),
        by_rep=by_rep,
    )
