"""Summaries' numbers (batch 15b; BRIEF D16): a team's issues in a period, ranked and marked
against their usual level. Every number comes from SQL here; the AI only writes the words, and
code checks them (batch 15b's later parts).

Only confirmed, visible insights count (an issue's routes are only theirs), and only issues with
routes (issues.with_routes, the one definition the inbox uses too). An issue's status in a past
period is read back from its history (IssueEvent), so a summary of W4 is right even after W5.

Per issue, in the period: shops (distinct), insights, reps and visits; shops raising it again
(two or more visits, up to the period's end); its age in whole weeks, from the start of the week
it was first seen to the period's start; overdue or not; its trend label; and its rank score,
severity weight (high 3, medium 2, low 1) x open shops x (1 + age in weeks).

The trend label, the first rule that holds:
1. resolved: resolved in the period, with nothing new since;
2. recurring: reopened by the period's end (it has been reopened, read from its history: not
   only in this period);
3. new: first seen in the period;
4. growing: GROWING_MIN_SHOPS shops or more, and the Poisson chance of that many at the
   baseline (the mean of shops per period over the previous BASELINE_PERIODS, floored at
   BASELINE_FLOOR) is under GROWING_MAX_P;
5. recurring: seen in RECURRING_MIN_PERIODS of the previous BASELINE_PERIODS periods;
6. steady.
The plan had both halves of `recurring` second; K6's P2 (seen three weeks running, then four
shops in W6) is `growing`, so the planner put the jump above the usual level first (2 Nov): an
issue that keeps coming back at its usual level is recurring, one that jumps above it, growing.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from math import exp, factorial
from typing import Any, Literal, get_args

from sqlalchemy import Select, distinct, exists, func, select
from sqlalchemy.orm import Session

from countertalk import entities, issues, limits
from countertalk.errors import Invalid
from countertalk.models import (
    ACTIVE_ISSUE,
    IST,
    CatalogItem,
    Insight,
    InsightRoute,
    Issue,
    IssueEvent,
    IssueEventField,
    IssueStatus,
    Shop,
    Team,
    Visit,
    ist_date,
    parse_id,
)
from countertalk.playbooks import OrgPack

PeriodKind = Literal["week", "month"]
Label = Literal["resolved", "recurring", "new", "growing", "steady"]
WEEK = timedelta(days=7)


# --- periods ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    """A week (Monday to Monday) or a calendar month, in IST, as UTC times: [start, end)."""

    kind: PeriodKind
    start: datetime
    end: datetime

    def holds(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def at_midnight(day: date) -> datetime:
    """The IST midnight a date starts at, in UTC."""
    return datetime.combine(day, time(), IST).astimezone(UTC)


def period_of(kind: PeriodKind, day: date) -> Period:
    """The week or month an IST date is in."""
    if kind == "week":
        monday = day - timedelta(days=day.weekday())
        return Period(kind, at_midnight(monday), at_midnight(monday + WEEK))
    first = day.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return Period(kind, at_midnight(first), at_midnight(following))


def previous(period: Period) -> Period:
    return period_of(period.kind, ist_date(period.start) - timedelta(days=1))


def last_complete(kind: PeriodKind, now: datetime) -> Period:
    """Last week or last month: the latest one that has ended."""
    return previous(period_of(kind, ist_date(now)))


def period_from(start: datetime, end: datetime) -> Period:
    """The week or month that is [start, end): a summary's, as stored."""
    for kind in get_args(PeriodKind):
        found = period_of(kind, ist_date(start))
        if (found.start, found.end) == (start, end):
            return found
    raise ValueError("not a week or a month")


def period_starting(kind: PeriodKind, day: date) -> Period:
    """The week (a Monday) or month (a 1st) starting on this IST date: 400 for any other day."""
    found = period_of(kind, day)
    if ist_date(found.start) != day:
        raise Invalid("not_a_period_start", f"That day doesn't start a {kind}")
    return found


def week_start(moment: datetime) -> datetime:
    return period_of("week", ist_date(moment)).start


def age_weeks(first_seen: datetime, period: Period) -> int:
    """Whole weeks from the start of the week it was first seen to the period's start."""
    return max(0, (period.start - week_start(first_seen)) // WEEK)


# --- the trend label and the score -----------------------------------------------------------


def poisson_at_least(count: int, rate: float) -> float:
    """The chance of `count` or more at a Poisson rate."""
    below = sum(exp(-rate) * rate**n / factorial(n) for n in range(count))
    return max(0.0, 1.0 - below)


def trend(
    *,
    shops: int,
    baseline: float,
    seen_periods: int,
    first_seen_in_period: bool,
    reopened: bool,
    resolved_in_period: bool,
) -> Label:
    """The first rule that holds (the module's docstring)."""
    if resolved_in_period:
        return "resolved"
    if reopened:
        return "recurring"
    if first_seen_in_period:
        return "new"
    rate = max(baseline, limits.BASELINE_FLOOR)
    if shops >= limits.GROWING_MIN_SHOPS and poisson_at_least(shops, rate) < limits.GROWING_MAX_P:
        return "growing"
    if seen_periods >= limits.RECURRING_MIN_PERIODS:
        return "recurring"
    return "steady"


def severity_weight(pack: OrgPack, severity: str) -> int:
    """The playbook's severities, highest first, weigh len … 1: high 3, medium 2, low 1; one it
    no longer has, 0."""
    return len(pack.playbook.severities) - issues.severity_rank(pack, severity)


# --- what an issue, or a cause, has in a period ----------------------------------------------


@dataclass(frozen=True)
class Measures:
    """An issue's numbers in a period, or a cause's across its teams (each insight and shop
    once)."""

    severity: str
    # In the period.
    shops: int
    insights: int
    reps: int
    visits: int
    # Up to the period's end: shops on two or more visits, and shops open since the latest
    # resolve.
    raised_again: int
    open_shops: int
    age_weeks: int
    overdue: bool
    baseline: float
    # Of the previous BASELINE_PERIODS periods, how many it was seen in.
    seen_periods: int
    label: Label
    score: int
    last_seen_at: datetime


@dataclass(frozen=True)
class IssueNumbers:
    issue_id: str
    title_en: str
    category: str
    # As it was at the period's end.
    status: str
    measures: Measures


@dataclass(frozen=True)
class Totals:
    """In the period: distinct insights, shops, reps and visits; for the org, the confirmed
    insights with no team (triage) too."""

    insights: int
    shops: int
    reps: int
    visits: int
    triage: int = 0


@dataclass(frozen=True)
class TeamNumbers:
    team_id: str
    period: Period
    issues: list[IssueNumbers]
    totals: Totals


@dataclass(frozen=True)
class _Member:
    insight_id: str
    visit_id: str
    shop_id: str
    rep_id: str
    seen_at: datetime
    severity: str
    area: str


@dataclass(frozen=True)
class _State:
    """An issue as it was at the period's end, read back from its history."""

    status: str
    reopened: bool
    resolved_in_period: bool
    # Where its latest run began: for an open one, its latest resolve (the shops open now are
    # the ones seen since); for a resolved one, the resolve before that.
    run_start: datetime | None
    overdue: bool

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_ISSUE


@dataclass(frozen=True)
class _Loaded:
    found: list[Issue]
    members: dict[str, list[_Member]]
    states: dict[str, _State]


def team_numbers(
    session: Session, pack: OrgPack, org_id: str, team_id: str, period: Period
) -> TeamNumbers:
    """The team's issues in the period, ranked: score, then the most recently seen; and its
    totals. An issue counts by its insights' visits, never by when it was filed."""
    loaded = _load(session, org_id, period, team_id)
    numbers = []
    for issue in loaded.found:
        part = (loaded.states[issue.id], loaded.members[issue.id])
        measured = _measure(pack, [part], period)
        if measured is not None:
            state = loaded.states[issue.id]
            numbers.append(
                IssueNumbers(issue.id, issue.title_en, issue.category, state.status, measured)
            )
    shown = [m for n in numbers for m in loaded.members[n.issue_id] if period.holds(m.seen_at)]
    return TeamNumbers(team_id, period, _ranked(numbers), totals_of(shown))


def _ranked[T: IssueNumbers | CauseNumbers](numbers: list[T]) -> list[T]:
    return sorted(numbers, key=lambda n: (-n.measures.score, -n.measures.last_seen_at.timestamp()))


def totals_of(in_period: Sequence[_Member], triage: int = 0) -> Totals:
    return Totals(
        insights=len({m.insight_id for m in in_period}),
        shops=len({m.shop_id for m in in_period}),
        reps=len({m.rep_id for m in in_period}),
        visits=len({m.visit_id for m in in_period}),
        triage=triage,
    )


@cache
def _earlier(period: Period) -> tuple[Period, ...]:
    found, current = [], period
    for _ in range(limits.BASELINE_PERIODS):
        current = previous(current)
        found.append(current)
    return tuple(found)


def shown_in(org_id: str, period: Period) -> tuple[Any, ...]:
    """The insights a summary counts: confirmed and shown, from the period's visits. Written
    only here (R1)."""
    return (
        Insight.org_id == org_id,
        Insight.visible.is_(True),
        Visit.created_at >= period.start,
        Visit.created_at < period.end,
    )


def _members(*leading: Any) -> Select[Any]:
    """Insights as _Members: their visit's shop, rep and time, and the shop's area."""
    return (
        select(*leading, Insight.id, Visit.id, Visit.shop_id, Visit.rep_id)
        .add_columns(Visit.created_at, Insight.severity, Shop.area)
        .select_from(Insight)
        .join(Visit, Visit.id == Insight.visit_id)
        .join(Shop, Shop.id == Visit.shop_id)
    )


def _load(
    session: Session,
    org_id: str,
    period: Period,
    team_id: str | None,
    issue_id: str | None = None,
) -> _Loaded:
    """The issues with routes that could matter to the period (open ones, and ones seen or
    resolved since the baseline's first period), of one team or all, that have an insight seen
    by its end; those insights; and their state at its end. One whose insights all came later
    has nothing to say for the period: as it stood then, it had none."""
    since = _earlier(period)[-1].start
    query = select(Issue).where(
        Issue.org_id == org_id,
        issues.with_routes(),
        issues.active() | (Issue.last_seen_at >= since) | (Issue.resolved_at >= since),
    )
    if team_id is not None:
        query = query.where(Issue.team_id == team_id)
    if issue_id is not None:  # one issue, for its drill-down
        query = query.where(Issue.id == issue_id)
    candidates = list(session.scalars(query))
    members: dict[str, list[_Member]] = defaultdict(list)
    for issue_id, *member in session.execute(
        _members(InsightRoute.issue_id)
        .join(InsightRoute, InsightRoute.insight_id == Insight.id)
        .where(
            InsightRoute.issue_id.in_([issue.id for issue in candidates]),
            Insight.visible.is_(True),
            Visit.created_at < period.end,
        )
    ):
        members[issue_id].append(_Member(*member))
    found = [issue for issue in candidates if members[issue.id]]
    ids = [issue.id for issue in found]
    history: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for issue_id, when, status in session.execute(
        select(IssueEvent.issue_id, IssueEvent.created_at, IssueEvent.to_value)
        .where(
            IssueEvent.issue_id.in_(ids),
            IssueEvent.field == IssueEventField.STATUS,
            IssueEvent.created_at < period.end,
        )
        .order_by(IssueEvent.created_at)
    ):
        history[issue_id].append((when, status))
    states = {issue.id: _state(issue, history[issue.id], period) for issue in found}
    return _Loaded(found, members, states)


def _state(issue: Issue, history: Sequence[tuple[datetime, str]], period: Period) -> _State:
    status, resolved_at, reopened = IssueStatus.OPEN.value, None, False
    resolves: list[datetime] = []
    for when, to in history:
        if to == IssueStatus.OPEN and status == IssueStatus.RESOLVED:
            reopened = True
        status = to
        resolved_at = when if to == IssueStatus.RESOLVED else None
        if to == IssueStatus.RESOLVED:
            resolves.append(when)
    active = status in ACTIVE_ISSUE
    # An open one's run began at its latest resolve; a resolved one's, at the resolve before.
    begun = resolves if active else resolves[:-1]
    return _State(
        status=status,
        reopened=reopened,
        resolved_in_period=resolved_at is not None and period.holds(resolved_at),
        run_start=begun[-1] if begun else None,
        # Today's due date: its history isn't kept.
        overdue=active and issue.due_at < period.end,
    )


def _run(parts: Sequence[tuple[_State, Sequence[_Member]]]) -> list[_Member]:
    """Its latest run: the open teams' issues' insights since their latest resolve; if none is
    open, each issue's up to the resolve that closed it. What open shops count, and what the
    drill-down lists (R1)."""
    running = [(state, part) for state, part in parts if state.active] or parts
    return [
        m
        for state, part in running
        for m in part
        if state.run_start is None or m.seen_at > state.run_start
    ]


@dataclass(frozen=True)
class Run:
    """One of a team's issues as its summary counts it (batch 15c's drill-down): its
    insights up to the period's end (members: the period's counts and raised again come
    from these) and its latest run of them (run: its open shops)."""

    issue: Issue
    status: str
    members: list[_Member]
    run: list[_Member]


def run_of(session: Session, org_id: str, team_id: str, issue_id: str, period: Period) -> Run:
    """The team's issue as its summary for the period counts it; 404 if it isn't one the
    summary lists (another team's, merged away, with nothing up to the period's end, or
    closed before it with nothing in it)."""
    found = parse_id(issue_id)
    loaded = None if found is None else _load(session, org_id, period, team_id, found)
    if loaded is None or not loaded.found:
        raise issues.missing_issue()
    [issue] = loaded.found
    state, members = loaded.states[issue.id], loaded.members[issue.id]
    in_period = [m for m in members if period.holds(m.seen_at)]
    if not _listed(in_period, state.active, state.resolved_in_period):
        raise issues.missing_issue()
    return Run(issue, state.status, members, _run([(state, members)]))


def _listed(in_period: Sequence[_Member], active: bool, resolved_in_period: bool) -> bool:
    """Whether a summary lists it: something in the period, or open, or resolved in it (R1:
    the summary and its drill-down both ask)."""
    return bool(in_period) or active or resolved_in_period


def raised_again_of(members: Sequence[_Member]) -> int:
    """Shops raising it again: two or more visits among these insights (R1: the summary and
    its drill-down both count it)."""
    visits_by_shop: dict[str, set[str]] = defaultdict(set)
    for m in members:
        visits_by_shop[m.shop_id].add(m.visit_id)
    return sum(1 for visits in visits_by_shop.values() if len(visits) >= 2)


def _measure(
    pack: OrgPack, parts: Sequence[tuple[_State, Sequence[_Member]]], period: Period
) -> Measures | None:
    """One issue's measures, or a cause's across its teams' issues (each insight once), from
    their insights seen by the period's end (_load leaves out an issue with none). None if it
    has nothing to say for the period: closed before it began, with nothing in it."""
    members = list({m.insight_id: m for _, part in parts for m in part}.values())
    states = [state for state, _ in parts]
    active = any(state.active for state in states)
    resolved_in_period = not active and any(state.resolved_in_period for state in states)
    in_period = [m for m in members if period.holds(m.seen_at)]
    if not _listed(in_period, active, resolved_in_period):
        return None
    earlier = _earlier(period)
    first_seen = min(m.seen_at for m in members)
    shops_per_period = [len({m.shop_id for m in members if p.holds(m.seen_at)}) for p in earlier]
    shops = len({m.shop_id for m in in_period})
    baseline = sum(shops_per_period) / len(shops_per_period)
    seen_periods = sum(1 for count in shops_per_period if count > 0)
    label = trend(
        shops=shops,
        baseline=baseline,
        seen_periods=seen_periods,
        first_seen_in_period=period.holds(first_seen),
        reopened=any(state.reopened for state in states),
        resolved_in_period=resolved_in_period,
    )
    run = _run(parts)
    open_shops = len({m.shop_id for m in run}) if active else 0
    # Its severity as issues.py keeps it: the highest of its latest run's insights (all of them,
    # if the ones that began the run have since been hidden).
    severity = min(
        (m.severity for m in run or members), key=lambda s: issues.severity_rank(pack, s)
    )
    age = age_weeks(first_seen, period)
    counted = totals_of(in_period)
    return Measures(
        severity=severity,
        shops=counted.shops,
        insights=counted.insights,
        reps=counted.reps,
        visits=counted.visits,
        raised_again=raised_again_of(members),
        open_shops=open_shops,
        age_weeks=age,
        overdue=any(state.overdue for state in states),
        baseline=baseline,
        seen_periods=seen_periods,
        label=label,
        score=severity_weight(pack, severity) * open_shops * (1 + age),
        last_seen_at=max(m.seen_at for m in members),
    )


# --- the org's numbers -----------------------------------------------------------------------


@dataclass(frozen=True)
class TeamWork:
    """A team working a cause, and its issue's status at the period's end."""

    team_key: str
    team_name: str
    issue_id: str
    status: str


@dataclass(frozen=True)
class CauseNumbers:
    """A cause across teams: the category and its key fields, whichever teams it went to, and
    any issue sharing an insight with them (_causes). Each insight and each shop counts once,
    even when it went to two teams. Its key and title are its biggest issue's."""

    issue_key: str
    title_en: str
    category: str
    teams: list[TeamWork]
    measures: Measures


@dataclass(frozen=True)
class CategoryArea:
    """Causes with insights in the period, and their shops, by category and the shops' area."""

    category: str
    area: str
    causes: int
    shops: int


@dataclass(frozen=True)
class CompetitorCount:
    """Insights in the period naming a competitor, and their shops."""

    competitor: str
    insights: int
    shops: int


@dataclass(frozen=True)
class OrgNumbers:
    period: Period
    causes: list[CauseNumbers]
    totals: Totals
    by_category_area: list[CategoryArea]
    competitors: list[CompetitorCount]
    # The teams' issues as they stood at the period's end, read back from their history: open,
    # in progress, and overdue (open or in progress, past today's due date). Counted apart: the
    # ones resolved in the period (R10).
    issues_at_end: dict[str, int]
    resolved_in_period: int


def org_numbers(session: Session, pack: OrgPack, org_id: str, period: Period) -> OrgNumbers:
    """The leadership summary's numbers: causes across teams, ranked like a team's issues."""
    loaded = _load(session, org_id, period, None)
    teams = {
        team_id: (key, name)
        for team_id, key, name in session.execute(
            select(Team.id, Team.key, Team.name).where(Team.org_id == org_id)
        )
    }
    causes = []
    grouped_causes = _causes(loaded)
    for grouped in grouped_causes:
        parts = [(loaded.states[i.id], loaded.members[i.id]) for i in grouped]
        measured = _measure(pack, parts, period)
        if measured is None:
            continue
        work = sorted(
            (TeamWork(*teams[i.team_id], i.id, loaded.states[i.id].status) for i in grouped),
            key=lambda w: w.team_key,
        )
        lead = grouped[0]
        causes.append(CauseNumbers(lead.issue_key, lead.title_en, lead.category, work, measured))
    triage = [
        _Member(*row)
        for row in session.execute(_members().where(*shown_in(org_id, period), _no_route()))
    ]
    routed = [m for i in loaded.found for m in loaded.members[i.id] if period.holds(m.seen_at)]
    return OrgNumbers(
        period=period,
        causes=_ranked(causes),
        # From what the summary shows (its causes' insights and triage's), so the checker's
        # fresh COUNT(DISTINCT) can disagree.
        totals=totals_of(routed + triage, triage=len(triage)),
        by_category_area=_by_category_area(loaded, grouped_causes, period),
        competitors=_competitors(session, org_id, period),
        issues_at_end=_at_end(loaded),
        resolved_in_period=sum(1 for i in loaded.found if loaded.states[i.id].resolved_in_period),
    )


def _causes(loaded: _Loaded) -> list[list[Issue]]:
    """The issues of each cause, its biggest first: issues with one key, and issues sharing an
    insight, joined transitively (an insight sent to two teams, or moved by batch 15c's merge
    into an issue with other key fields), so no insight is in two causes."""
    parent = {issue.id: issue.id for issue in loaded.found}

    def root(issue_id: str) -> str:
        while parent[issue_id] != issue_id:
            parent[issue_id] = parent[parent[issue_id]]
            issue_id = parent[issue_id]
        return issue_id

    first: dict[str, str] = {}
    for issue in loaded.found:
        links = [f"key {issue.issue_key}"]
        links += [f"insight {m.insight_id}" for m in loaded.members[issue.id]]
        for link in links:
            if link in first:
                parent[root(issue.id)] = root(first[link])
            else:
                first[link] = issue.id
    grouped: dict[str, list[Issue]] = defaultdict(list)
    for issue in loaded.found:
        grouped[root(issue.id)].append(issue)
    size = {issue.id: len(loaded.members[issue.id]) for issue in loaded.found}
    return [
        sorted(issues_of, key=lambda i: (-size[i.id], i.issue_key, i.id))
        for issues_of in grouped.values()
    ]


def _no_route() -> Any:
    """An insight sent to no team: triage's."""
    return ~exists().where(InsightRoute.insight_id == Insight.id)


def _by_category_area(
    loaded: _Loaded, grouped_causes: list[list[Issue]], period: Period
) -> list[CategoryArea]:
    # Each issue's cause, by its lead issue's key: the causes' own grouping (_causes).
    cause_of = {i.id: grouped[0].issue_key for grouped in grouped_causes for i in grouped}
    causes: dict[tuple[str, str], set[str]] = defaultdict(set)
    shops: dict[tuple[str, str], set[str]] = defaultdict(set)
    for issue in loaded.found:
        for m in loaded.members[issue.id]:
            if period.holds(m.seen_at):
                where = (issue.category, m.area)
                causes[where].add(cause_of[issue.id])
                shops[where].add(m.shop_id)
    return [
        CategoryArea(category, area, len(causes[(category, area)]), len(shops[(category, area)]))
        for category, area in sorted(causes)
    ]


def _competitors(session: Session, org_id: str, period: Period) -> list[CompetitorCount]:
    rows = session.execute(
        select(CatalogItem.brand, CatalogItem.product)
        .add_columns(func.count(distinct(Insight.id)), func.count(distinct(Visit.shop_id)))
        .join(Insight, Insight.competitor_id == CatalogItem.id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(*shown_in(org_id, period))
        .group_by(CatalogItem.brand, CatalogItem.product)
    )
    found = [
        CompetitorCount(entities.item_name(brand, product), insights, shops)
        for brand, product, insights, shops in rows
    ]
    return sorted(found, key=lambda c: (-c.insights, c.competitor))


def _at_end(loaded: _Loaded) -> dict[str, int]:
    seen = [loaded.states[i.id] for i in loaded.found]
    return {
        "open": sum(1 for s in seen if s.status == IssueStatus.OPEN),
        "in_progress": sum(1 for s in seen if s.status == IssueStatus.IN_PROGRESS),
        "overdue": sum(1 for s in seen if s.overdue),
    }


# --- the checker -----------------------------------------------------------------------------


def check(session: Session, org_id: str, numbers: TeamNumbers | OrgNumbers) -> list[str]:
    """Each rule a summary's numbers must keep, against fresh SQL counts: what breaks, or none.
    Every route of a confirmed insight from the period is in an issue (the composite foreign key
    keeps that issue in the route's team); a team's totals are its issues', each insight once;
    the org's totals are a fresh COUNT(DISTINCT), never the sum of the teams' totals, with
    triage beside them."""
    period = numbers.period
    team_id = numbers.team_id if isinstance(numbers, TeamNumbers) else None
    problems = []
    routes = (
        select(func.count())
        .select_from(InsightRoute)
        .join(Insight, Insight.id == InsightRoute.insight_id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(*shown_in(org_id, period), InsightRoute.issue_id.is_(None))
    )
    if team_id is not None:
        routes = routes.where(InsightRoute.team_id == team_id)
    stray = session.scalar(routes) or 0
    if stray:
        problems.append(f"{stray} routes of the period's insights in no issue of their team")
    fresh = _fresh(session, org_id, period, team_id)
    if isinstance(numbers, TeamNumbers):
        if (numbers.totals.insights, numbers.totals.shops) != fresh:
            problems.append(f"the team's totals {numbers.totals} aren't a fresh count {fresh}")
        if sum(n.measures.insights for n in numbers.issues) != numbers.totals.insights:
            problems.append("the team's insights aren't its issues' insights")
        return problems
    if (numbers.totals.insights, numbers.totals.shops) != fresh:
        problems.append(f"the org's totals {numbers.totals} aren't a fresh count {fresh}")
    triage = session.scalar(
        select(func.count(distinct(Insight.id)))
        .join(Visit, Visit.id == Insight.visit_id)
        .where(*shown_in(org_id, period), _no_route())
    )
    if numbers.totals.triage != triage:
        problems.append("the org's triage isn't a fresh count")
    routed = sum(c.measures.insights for c in numbers.causes)
    if routed + numbers.totals.triage != numbers.totals.insights:
        problems.append("the causes and triage don't account for each insight once")
    return problems


def _fresh(session: Session, org_id: str, period: Period, team_id: str | None) -> tuple[int, int]:
    """COUNT(DISTINCT) insights and shops: the period's confirmed, shown insights; for a team,
    those routed to it."""
    query = (
        select(func.count(distinct(Insight.id)), func.count(distinct(Visit.shop_id)))
        .select_from(Insight)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(*shown_in(org_id, period))
    )
    if team_id is not None:
        query = query.join(InsightRoute, InsightRoute.insight_id == Insight.id).where(
            InsightRoute.team_id == team_id
        )
    insights, shops = session.execute(query).one()
    return insights, shops
