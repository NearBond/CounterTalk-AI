"""Who is asking. Until batch 16 adds sign-in, every request is the demo rep's."""

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
    user = session.scalars(select(User).where(User.email == DEMO_REP_EMAIL)).one_or_none()
    if user is None:
        raise PermanentError(
            "demo_rep_missing", "The demo rep isn't set up: run scripts/seed_dev.py"
        )
    return CurrentUser(user.id, user.org_id, user.role)
