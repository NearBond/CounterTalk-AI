"""Who is asking (batch 16): the signed-in user, from their session (countertalk.auth)."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk import auth, inbox, rows
from countertalk.errors import Invalid, NotFound
from countertalk.models import Org, Role, Team, User, parse_id


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


# Made-up users only (scripts/add_user.py): real people's emails never go in by hand.
EXAMPLE_DOMAIN = "@example.com"


def add_user(
    session: Session,
    org_id: str,
    email: str,
    name: str,
    role: str,
    team_key: str | None,
    password: str,
) -> User | None:
    """A new user of the org with a password, to sign in as (batch 16): None if a user has the
    email already, and nothing changes. Only example.com emails; a team member's team by its
    key, and only a team member has one."""
    email = auth.normal_email(email)
    if not email.endswith(EXAMPLE_DOMAIN) or email == EXAMPLE_DOMAIN:
        raise Invalid("example_email_only", "Only made-up users here: an example.com email")
    if role not in set(Role):
        raise Invalid("unknown_role", f"The role is one of {', '.join(Role)}")
    if (role == Role.TEAM_MEMBER) != (team_key is not None):
        raise Invalid("team_member_team", "A team member needs --team, and only a team member")
    auth.long_enough(password)
    found = parse_id(org_id)
    if found is None or session.get(Org, found) is None:
        raise NotFound("org_not_found", "There's no such org")
    if session.scalar(select(User.id).where(User.email == email)) is not None:
        return None
    team_id = None if team_key is None else inbox.team_of(session, found, team_key).id
    user = User(
        org_id=found,
        email=email,
        name=name,
        role=role,
        team_id=team_id,
        password_hash=auth.hash_password(password),
    )
    session.add(user)
    session.flush()
    return user


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
