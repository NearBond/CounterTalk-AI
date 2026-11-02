"""Who may see and do what (batch 16; CODING_GUIDELINES §7.2). Every route on org data asks
here before it reads or writes; the domain modules then load what they need by org (rows.find_one).

- A rep sees their own visits: records them, reviews and confirms them. Through those, they see
  what came of their own insights (each route's team, status and note), and nothing else of a
  team's.
- A team member sees their team's inbox, issues, merge suggestions, summaries (team scope, their
  team) and drill-down, and acts on them. They may read a confirmed visit with at least one
  insight routed to their team, so its citations and drill-down open: its transcript and
  recording, and of its review only those insights, with only their team's routes; never
  the rep's notes, report, tasks or order (review.team_part). They change nothing of it.
  Before confirm, a visit's routes are only suggestions: it's the rep's alone.
- A manager sees the whole org and acts on its issues and triage; an admin, the same (16b adds
  the catalog). Neither records visits or changes a rep's review.

Another org's object, or anything the user's role may not see, is 404, never 403, which would
say it exists (§2.10). A change the role may not make, to something the user can see, is 403.
A route the role can't use at all is 404 not_found.
"""

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from countertalk import inbox, issues, merges, review, routing, rows, summary_jobs, visits
from countertalk.errors import Forbidden, Invalid, NotFound
from countertalk.models import (
    Insight,
    InsightRoute,
    Issue,
    IssueMergeSuggestion,
    Role,
    Summary,
    SummaryScope,
    Team,
    Visit,
    VisitStatus,
)
from countertalk.users import CurrentUser

# Who sees the whole org.
WHOLE_ORG = (Role.MANAGER, Role.ADMIN)


def nothing_here() -> NotFound:
    return NotFound("not_found", "There's nothing here")


def only(user: CurrentUser, *roles: str) -> None:
    """A route only these roles use: 404 not_found for any other."""
    if user.role not in roles:
        raise nothing_here()


def whole_org(user: CurrentUser) -> bool:
    return user.role in WHOLE_ORG


# --- visits and their insights ---------------------------------------------------------------


def visits_of(user: CurrentUser) -> str | None:
    """Whose visits the list shows: a rep's own (their ID), or the whole org's (None)."""
    only(user, Role.REP, *WHOLE_ORG)
    return user.id if user.role == Role.REP else None


def visit(
    session: Session, user: CurrentUser, visit_id: str, *, change: bool = False
) -> str | None:
    """The visit, if the user sees it (404 otherwise); to change it, only its rep (403). Which
    part they see: None for all of it (its rep, a manager or admin), or a team member's team
    ID, for only what was routed there (review.team_part)."""
    found = rows.find_one(session, Visit, user.org_id, visit_id, visits.missing_visit())
    if found.rep_id == user.id:
        return None
    if not (whole_org(user) or _routed_to_their_team(session, user, found.id)):
        raise visits.missing_visit()
    if change:
        raise Forbidden("not_your_visit", "Only the visit's rep can change it")
    return None if whole_org(user) else user.team_id


def insight(session: Session, user: CurrentUser, insight_id: str, *, change: bool = False) -> None:
    """The insight, if the user sees its visit; to change it in review, only the visit's rep."""
    found = rows.find_one(session, Insight, user.org_id, insight_id, review.missing_insight())
    try:
        visit(session, user, found.visit_id, change=change)
    except NotFound:
        raise review.missing_insight() from None  # the insight's own code, as for none at all


def route(session: Session, user: CurrentUser, insight_id: str, slot: int) -> None:
    """Acting on an insight's route in a slot (accept, move, also send): a member of the route's
    team, or a manager or admin. 403 for anyone else who sees the insight."""
    insight(session, user, insight_id)
    if whole_org(user):
        return
    team_id = session.scalar(
        select(InsightRoute.team_id).where(
            InsightRoute.org_id == user.org_id,
            InsightRoute.insight_id == insight_id,
            InsightRoute.slot == slot,
        )
    )
    if user.role != Role.TEAM_MEMBER or team_id is None or team_id != user.team_id:
        raise Forbidden("not_your_team", "Only its team can act on this finding's route")


def triage(session: Session, user: CurrentUser, insight_id: str) -> None:
    """Giving an insight in triage a team: managers and admins. 403 for anyone else who sees it."""
    insight(session, user, insight_id)
    if not whole_org(user):
        raise Forbidden("not_a_manager", "Only a manager gives a team to a finding in triage")


def _routed_to_their_team(session: Session, user: CurrentUser, visit_id: str) -> bool:
    if user.role != Role.TEAM_MEMBER or user.team_id is None:
        return False
    return bool(
        session.scalar(
            select(
                exists().where(
                    InsightRoute.org_id == user.org_id,
                    InsightRoute.team_id == user.team_id,
                    InsightRoute.insight_id == Insight.id,
                    Insight.visit_id == Visit.id,
                    Visit.id == visit_id,
                    Visit.status == VisitStatus.CONFIRMED,
                )
            )
        )
    )


# --- teams, issues and summaries -------------------------------------------------------------


def team(session: Session, user: CurrentUser, key: str | None) -> Team:
    """The inbox's team: a team member's own (another key is 404); a manager or admin names one
    (400 team_required without)."""
    if user.role == Role.TEAM_MEMBER and user.team_id is not None:
        own = session.get_one(Team, user.team_id)
        if key is not None and key != own.key:
            raise routing.missing_team()
        return own
    only(user, *WHOLE_ORG)
    if key is None:
        raise Invalid("team_required", "Pick a team")
    return inbox.team_of(session, user.org_id, key)


def team_id(user: CurrentUser, wanted: str) -> None:
    """A team's summary or drill-down by its ID: its member, or a manager or admin (404)."""
    if whole_org(user):
        return
    if user.role != Role.TEAM_MEMBER or wanted != user.team_id:
        raise routing.missing_team()


def team_work(user: CurrentUser) -> None:
    """The inbox's choices: anyone who works issues."""
    only(user, Role.TEAM_MEMBER, *WHOLE_ORG)


def issue(session: Session, user: CurrentUser, issue_id: str) -> None:
    """An issue: its team's member, or a manager or admin; 404 issue_not_found for anyone
    else, a rep too, as for an issue that doesn't exist."""
    found = rows.find_one(session, Issue, user.org_id, issue_id, issues.missing_issue())
    if not _works(user, found.team_id):
        raise issues.missing_issue()


def suggestion(session: Session, user: CurrentUser, suggestion_id: str) -> None:
    """A merge suggestion: its team's member, or a manager or admin (404 for anyone else)."""
    found = rows.find_one(
        session, IssueMergeSuggestion, user.org_id, suggestion_id, merges.missing_suggestion()
    )
    if not _works(user, found.team_id):
        raise merges.missing_suggestion()


def _works(user: CurrentUser, team_id: str) -> bool:
    """Whether the user works this team's issues: its member, or a manager or admin."""
    return whole_org(user) or (user.role == Role.TEAM_MEMBER and user.team_id == team_id)


def summary_of(user: CurrentUser, scope: str, wanted: str | None) -> None:
    """A summary of this scope and team: a team's, its member or a manager or admin; the org's,
    a manager or admin."""
    team_work(user)
    if scope == SummaryScope.ORG:
        only(user, *WHOLE_ORG)
    elif wanted is not None:
        team_id(user, wanted)


def summary(session: Session, user: CurrentUser, summary_id: str) -> None:
    """A summary by its ID, as summary_of says: another's is 404 summary_not_found."""
    found = rows.find_one(session, Summary, user.org_id, summary_id, summary_jobs.missing_summary())
    try:
        summary_of(user, found.scope, found.team_id)
    except NotFound:
        raise summary_jobs.missing_summary() from None
