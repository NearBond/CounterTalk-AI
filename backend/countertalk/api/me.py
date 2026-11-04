"""/api/me: the signed-in rep's own numbers (batch 17b), and what got fixed for every shop since
its last visit, for the rep's Updates page (batch 17e)."""

from fastapi import APIRouter
from pydantic import BaseModel

from countertalk import access, fixed
from countertalk.api.deps import CurrentUser
from countertalk.api.shops import SHOP_STAFF, FixedOut
from countertalk.db import DbSession
from countertalk.models import Role

router = APIRouter(prefix="/api/me", tags=["me"])


class CountsOut(BaseModel):
    # Problems the rep raised this IST month, from their own confirmed visits.
    raised: int
    # Of those, the ones whose every routed issue is resolved now.
    fixed: int


class ShopFixedOut(BaseModel):
    """One shop on Updates: what got fixed since its last visit here, newest first."""

    shop_id: str
    name: str
    area: str
    items: list[FixedOut]


class ShopsFixedPage(BaseModel):
    # The shops, newest fix first.
    items: list[ShopFixedOut]
    next_cursor: str | None = None


@router.get("/fixed")
def fixed_everywhere(user: CurrentUser, session: DbSession) -> ShopsFixedPage:
    """Every shop of the org with something fixed since its last visit, in one call."""
    access.only(user, *SHOP_STAFF)
    found = fixed.every_shop(session, user.org_id)
    return ShopsFixedPage(
        items=[ShopFixedOut.model_validate(shop, from_attributes=True) for shop in found]
    )


class HomeOut(BaseModel):
    """The rep's home's counts (batch F2)."""

    # The rep's own visits waiting for review.
    to_review: int
    # Shops with something fixed since their last visit: Updates' sections.
    to_pass_on: int


@router.get("/home")
def my_home(user: CurrentUser, session: DbSession) -> HomeOut:
    """A rep's own: their visits only, never another rep's, never a ranking."""
    access.only(user, Role.REP)
    found = fixed.home(session, user.org_id, user.id)
    return HomeOut(to_review=found.to_review, to_pass_on=found.to_pass_on)


@router.get("/counts")
def my_counts(user: CurrentUser, session: DbSession) -> CountsOut:
    """A rep's own counts: never another rep's, never a ranking."""
    access.only(user, Role.REP)
    found = fixed.my_counts(session, user.org_id, user.id)
    return CountsOut(raised=found.raised, fixed=found.fixed)
