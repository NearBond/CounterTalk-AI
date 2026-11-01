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
from math import exp, factorial
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk import issues, limits
from countertalk.models import (
    ACTIVE_ISSUE,
    IST,
    Insight,
    InsightRoute,
    Issue,
    IssueEvent,
    IssueEventField,
    IssueStatus,
    Visit,
    ist_date,
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


def _at_midnight(day: date) -> datetime:
    return datetime.combine(day, time(), IST).astimezone(UTC)


def period_of(kind: PeriodKind, day: date) -> Period:
    """The week or month an IST date is in."""
    if kind == "week":
        monday = day - timedelta(days=day.weekday())
        return Period(kind, _at_midnight(monday), _at_midnight(monday + WEEK))
    first = day.replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    return Period(kind, _at_midnight(first), _at_midnight(following))


def previous(period: Period) -> Period:
    return period_of(period.kind, ist_date(period.start) - timedelta(days=1))


def last_complete(kind: PeriodKind, now: datetime) -> Period:
    """Last week or last month: the latest one that has ended."""
    return previous(period_of(kind, ist_date(now)))


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


# --- a team's numbers ------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueNumbers:
    issue_id: str
    title_en: str
    category: str
    severity: str
    # As it was at the period's end.
    status: str
    # In the period.
    shops: int
    insights: int
    reps: int
    visits: int
    # Up to the period's end: shops on two or more visits, and shops while it's open.
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
class _Member:
    insight_id: str
    visit_id: str
    shop_id: str
    rep_id: str
    seen_at: datetime
    severity: str


def team_numbers(
    session: Session, pack: OrgPack, org_id: str, team_id: str, period: Period
) -> list[IssueNumbers]:
    """The team's issues in the period, ranked: score, then the most recently seen. An issue
    counts by its insights' visits, never by when it was filed. Only issues that could matter
    to it are read: open ones, and ones seen or resolved since the baseline's first period."""
    earlier = _earlier(period)
    since = earlier[-1].start
    found = session.scalars(
        select(Issue).where(
            Issue.org_id == org_id,
            Issue.team_id == team_id,
            issues.with_routes(),
            Issue.status.in_([status.value for status in ACTIVE_ISSUE])
            | (Issue.last_seen_at >= since)
            | (Issue.resolved_at >= since),
        )
    ).all()
    ids = [issue.id for issue in found]
    members: dict[str, list[_Member]] = defaultdict(list)
    for issue_id, *member in session.execute(
        select(InsightRoute.issue_id, Insight.id, Visit.id, Visit.shop_id, Visit.rep_id)
        .add_columns(Visit.created_at, Insight.severity)
        .join(Insight, Insight.id == InsightRoute.insight_id)
        .join(Visit, Visit.id == Insight.visit_id)
        .where(
            InsightRoute.issue_id.in_(ids),
            Insight.visible.is_(True),
            Visit.created_at < period.end,
        )
    ):
        members[issue_id].append(_Member(*member))
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
    numbers = []
    for issue in found:
        counted = _numbers(pack, issue, members[issue.id], history[issue.id], period, earlier)
        if counted is not None:
            numbers.append(counted)
    return sorted(numbers, key=lambda n: (-n.score, -n.last_seen_at.timestamp()))


def _earlier(period: Period) -> list[Period]:
    found, current = [], period
    for _ in range(limits.BASELINE_PERIODS):
        current = previous(current)
        found.append(current)
    return found


def _numbers(
    pack: OrgPack,
    issue: Issue,
    members: Sequence[_Member],
    history: Sequence[tuple[datetime, str]],
    period: Period,
    earlier: Sequence[Period],
) -> IssueNumbers | None:
    """One issue's numbers, or None if it has nothing to say for the period: no insight seen by
    its end, or closed before it began with nothing in it."""
    if not members:
        return None
    status, resolved_at, reopened = IssueStatus.OPEN.value, None, False
    # The latest resolve, kept through a reopen: the shops open now are the ones seen since.
    last_resolve: datetime | None = None
    for when, to in history:
        if to == IssueStatus.OPEN and status == IssueStatus.RESOLVED:
            reopened = True
        status = to
        resolved_at = when if to == IssueStatus.RESOLVED else None
        last_resolve = when if to == IssueStatus.RESOLVED else last_resolve
    in_period = [m for m in members if period.holds(m.seen_at)]
    active = status in ACTIVE_ISSUE
    resolved_in_period = resolved_at is not None and period.holds(resolved_at)
    if not in_period and not active and not resolved_in_period:
        return None
    first_seen = min(m.seen_at for m in members)
    last_seen = max(m.seen_at for m in members)
    visits_by_shop: dict[str, set[str]] = defaultdict(set)
    for m in members:
        visits_by_shop[m.shop_id].add(m.visit_id)
    shops_per_period = [len({m.shop_id for m in members if p.holds(m.seen_at)}) for p in earlier]
    shops = len({m.shop_id for m in in_period})
    baseline = sum(shops_per_period) / len(shops_per_period)
    seen_periods = sum(1 for count in shops_per_period if count > 0)
    label = trend(
        shops=shops,
        baseline=baseline,
        seen_periods=seen_periods,
        first_seen_in_period=period.holds(first_seen),
        reopened=reopened,
        resolved_in_period=resolved_in_period,
    )
    since_resolve = [m for m in members if last_resolve is None or m.seen_at > last_resolve]
    open_shops = len({m.shop_id for m in since_resolve}) if active else 0
    # Its severity as it was: the highest of its insights since the latest resolve.
    severity = min(
        (m.severity for m in since_resolve or members),
        key=lambda s: issues.severity_rank(pack, s),
    )
    age = age_weeks(first_seen, period)
    return IssueNumbers(
        issue_id=issue.id,
        title_en=issue.title_en,
        category=issue.category,
        severity=severity,
        status=status,
        shops=shops,
        insights=len({m.insight_id for m in in_period}),
        reps=len({m.rep_id for m in in_period}),
        visits=len({m.visit_id for m in in_period}),
        raised_again=sum(1 for visits in visits_by_shop.values() if len(visits) >= 2),
        open_shops=open_shops,
        age_weeks=age,
        overdue=active and issue.due_at < period.end,
        baseline=baseline,
        seen_periods=seen_periods,
        label=label,
        score=severity_weight(pack, severity) * open_shops * (1 + age),
        last_seen_at=last_seen,
    )
