"""/api/me: the signed-in rep's own numbers (batch 17b)."""

from fastapi import APIRouter
from pydantic import BaseModel

from countertalk import access, fixed
from countertalk.api.deps import CurrentUser
from countertalk.db import DbSession
from countertalk.models import Role

router = APIRouter(prefix="/api/me", tags=["me"])


class CountsOut(BaseModel):
    # Problems the rep raised this IST month, from their own confirmed visits.
    raised: int
    # Of those, the ones whose every routed issue is resolved now.
    fixed: int


@router.get("/counts")
def my_counts(user: CurrentUser, session: DbSession) -> CountsOut:
    """A rep's own counts: never another rep's, never a ranking."""
    access.only(user, Role.REP)
    found = fixed.my_counts(session, user.org_id, user.id)
    return CountsOut(raised=found.raised, fixed=found.fixed)
