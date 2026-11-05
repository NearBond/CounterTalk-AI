"""The overview (GET /api/me/overview): the counts at the top of the home of whoever works issues.
A manager or admin counts the whole org; a team member, only their own team's issues.

- **Issues** are counted as the inbox shows them: only those with routes (issues.with_routes:
  one emptied by moves isn't), each by its status now, so a merged one never is (R1, R10).
  - Open, and in progress: the issues with that status.
  - Overdue: open or in progress (ACTIVE_ISSUE), and past their due date.
  - Resolved this week: resolved now, at or after this IST week's Monday 00:00 (summaries'
    week). One reopened since isn't: it's open again.
- **Triage** (the whole org's only): the confirmed insights with no team, as the triage list
  holds them (inbox.in_triage).
- **Visits this week** (the whole org's only): the org's visits made since that Monday, failed
  ones aside.
- **Names to review** (an admin's only): the names linking couldn't place, waiting on the
  catalog screen: one per kind and name, however many visits said it, as its review queue
  lists them (catalog_admin.open_reviews).

A count the user's role doesn't see is None, never a 0 that would read as "nothing to do".

The reps (GET /api/me/reps, the Triage page's cards, a manager's or admin's): every field sales
rep of the org, by name, with no visits too. Each with their visits confirmed, their visits in
review, and the issues pending: those with an insight from one of their visits, open or in
progress now (so never a resolved or merged one), each counted once.
"""

from dataclasses import dataclass

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from countertalk import access, catalog_admin, inbox, issues, summaries
from countertalk.models import (
    ACTIVE_ISSUE,
    Insight,
    InsightRoute,
    Issue,
    IssueStatus,
    Role,
    User,
    Visit,
    VisitStatus,
    utc_now,
)
from countertalk.users import CurrentUser


@dataclass(frozen=True)
class Overview:
    open: int
    in_progress: int
    overdue: int
    resolved_this_week: int
    # The whole org's only: None for a team member.
    triage: int | None
    visits_this_week: int | None
    # An admin's only.
    names_to_review: int | None


def overview(session: Session, user: CurrentUser) -> Overview:
    """The user's counts: a team member's, their team's issues; a manager's or admin's, the
    whole org's, with triage and this week's visits, and an admin's the names to review too.
    Whether they may ask at all is access.team_work's."""
    now = utc_now()
    monday = summaries.week_start(now)
    whole = access.whole_org(user)
    shown = [Issue.org_id == user.org_id, issues.with_routes()]
    if not whole:
        shown.append(Issue.team_id == user.team_id)
    still_open, started, overdue, resolved = session.execute(
        select(
            func.count().filter(Issue.status == IssueStatus.OPEN),
            func.count().filter(Issue.status == IssueStatus.IN_PROGRESS),
            func.count().filter(Issue.status.in_(ACTIVE_ISSUE), Issue.due_at < now),
            func.count().filter(Issue.status == IssueStatus.RESOLVED, Issue.resolved_at >= monday),
        )
        .select_from(Issue)
        .where(*shown)
    ).one()
    triage = visits = names = None
    if whole:
        in_triage = select(func.count()).select_from(Insight).where(*inbox.in_triage(user.org_id))
        triage = session.scalar(in_triage) or 0
        made = select(func.count()).where(
            Visit.org_id == user.org_id,
            Visit.created_at >= monday,
            # Made: never failed, and not a recording that was never sent.
            Visit.status.not_in([VisitStatus.FAILED, VisitStatus.RECORDING]),
        )
        visits = session.scalar(made) or 0
    if user.role == Role.ADMIN:
        names = catalog_admin.open_review_count(session, user.org_id)
    return Overview(still_open, started, overdue, resolved, triage, visits, names)


@dataclass(frozen=True)
class RepCounts:
    rep_id: str
    rep_name: str
    confirmed: int
    in_review: int
    pending: int


def rep_counts(session: Session, org_id: str) -> list[RepCounts]:
    """The org's reps, by name: one query for them, one for their visits and one for their
    issues, each grouped by rep, never one per rep."""
    reps = select(User.id, User.name).where(User.org_id == org_id, User.role == Role.REP)
    shown = (VisitStatus.CONFIRMED, VisitStatus.REVIEW)
    visits = (
        select(Visit.rep_id, Visit.status, func.count())
        .where(Visit.org_id == org_id, Visit.status.in_(shown))
        .group_by(Visit.rep_id, Visit.status)
    )
    pending = (
        select(Visit.rep_id, func.count(distinct(Issue.id)))
        .join(Insight, Insight.visit_id == Visit.id)
        .join(InsightRoute, InsightRoute.insight_id == Insight.id)
        .join(Issue, Issue.id == InsightRoute.issue_id)
        .where(Visit.org_id == org_id, Issue.status.in_(ACTIVE_ISSUE))
        .group_by(Visit.rep_id)
    )
    counted = {(rep_id, status): count for rep_id, status, count in session.execute(visits)}
    issues_of = {rep_id: count for rep_id, count in session.execute(pending)}
    return [
        RepCounts(
            rep_id,
            name,
            counted.get((rep_id, VisitStatus.CONFIRMED), 0),
            counted.get((rep_id, VisitStatus.REVIEW), 0),
            issues_of.get(rep_id, 0),
        )
        for rep_id, name in session.execute(reps.order_by(User.name, User.id))
    ]
