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
"""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from countertalk import access, catalog_admin, inbox, issues, summaries
from countertalk.models import (
    ACTIVE_ISSUE,
    Insight,
    Issue,
    IssueStatus,
    Role,
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
            Visit.status != VisitStatus.FAILED,
        )
        visits = session.scalar(made) or 0
    if user.role == Role.ADMIN:
        names = len(catalog_admin.open_reviews(session, user.org_id))
    return Overview(still_open, started, overdue, resolved, triage, visits, names)
