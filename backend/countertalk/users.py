"""Who is asking (batch 16): the signed-in user, from their session (countertalk.auth)."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk import auth, rows
from countertalk.models import Org, Team, User


@dataclass(frozen=True)
class CurrentUser:
    id: str
    org_id: str
    role: str
    # A team member's own team; None for every other role.
    team_id: str | None


@dataclass(frozen=True)
class TeamOf:
    id: str
    key: str
    name: str


@dataclass(frozen=True)
class Me:
    """What the web app shows of the signed-in user."""

    id: str
    name: str
    email: str
    role: str
    team: TeamOf | None
    org_name: str


def signed_in(session: Session, token: str) -> CurrentUser | None:
    """The session's user while it lasts; None once it has ended or never was."""
    user = auth.user_of(session, token)
    return None if user is None else CurrentUser(user.id, user.org_id, user.role, user.team_id)


def me(session: Session, org_id: str, user_id: str) -> Me:
    user = rows.find_one(session, User, org_id, user_id, auth.missing_user())
    org_name = session.scalars(select(Org.name).where(Org.id == user.org_id)).one()
    team = None
    if user.team_id is not None:
        found = session.get_one(Team, user.team_id)
        team = TeamOf(found.id, found.key, found.name)
    return Me(user.id, user.name, user.email, user.role, team, org_name)
