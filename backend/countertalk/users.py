"""Who is asking. Until batch 16 adds sign-in, every request is the demo rep's, or in
development, DEV_USER_EMAIL's user (batch 15b)."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk.errors import PermanentError
from countertalk.models import User

# scripts/seed_dev.py creates this rep.
DEMO_REP_EMAIL = "rep@example.com"


@dataclass(frozen=True)
class CurrentUser:
    id: str
    org_id: str
    role: str


def demo_rep(session: Session) -> CurrentUser:
    return _by_email(
        session,
        DEMO_REP_EMAIL,
        PermanentError("demo_rep_missing", "The demo rep isn't set up: run scripts/seed_dev.py"),
    )


def dev_user(session: Session, email: str) -> CurrentUser:
    """DEV_USER_EMAIL's user (development only: the settings refuse it without DEBUG)."""
    return _by_email(
        session,
        email,
        PermanentError("dev_user_missing", "No user has DEV_USER_EMAIL's address: check .env"),
    )


def _by_email(session: Session, email: str, missing: PermanentError) -> CurrentUser:
    user = session.scalars(select(User).where(User.email == email)).one_or_none()
    if user is None:
        raise missing
    return CurrentUser(user.id, user.org_id, user.role)
