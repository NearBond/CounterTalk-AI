"""/api/me: the signed-in rep's own numbers (batch 17b), and what got fixed for every shop since
its last visit, for the rep's Updates page (batch 17e). The overview: the home's counts for whoever
works issues, and the reps' counts on Triage (overview.py)."""

from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from countertalk import access, fixed, overview
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
    """One shop on Updates: what got fixed since its last visit here, and (batch F7) the rep's own
    problems this month fixed before it, newest first."""

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
    """Every shop of the org with something fixed since its last visit, and every fix the
    rep's counts count (batch F7), in one call."""
    access.only(user, *SHOP_STAFF)
    found = fixed.for_rep(session, user.org_id, user.id)
    return ShopsFixedPage(
        items=[ShopFixedOut.model_validate(shop, from_attributes=True) for shop in found]
    )


class HomeOut(BaseModel):
    """The rep's home's counts (batch F2)."""

    # The rep's own visits waiting for review.
    to_review: int
    # Shops with a fix since their last visit: Updates' "Fixed since the last visit here".
    to_pass_on: int


@router.get("/home")
def my_home(user: CurrentUser, session: DbSession) -> HomeOut:
    """A rep's own: their visits only, never another rep's, never a ranking."""
    access.only(user, Role.REP)
    found = fixed.home(session, user.org_id, user.id)
    return HomeOut(to_review=found.to_review, to_pass_on=found.to_pass_on)


class PendingOut(BaseModel):
    """One of the rep's problems still being worked on (batch F3): never a rep's name or a quote."""

    # Its issue: what the page keys a row by.
    issue_id: str
    shop: str
    area: str
    team: str
    problem: str
    # open or in_progress
    status: str
    raised_at: datetime


class PendingPage(BaseModel):
    # Newest first.
    items: list[PendingOut]
    next_cursor: str | None = None


@router.get("/pending")
def my_pending(user: CurrentUser, session: DbSession) -> PendingPage:
    """A rep's own problems whose issue is still open or in progress."""
    access.only(user, Role.REP)
    found = fixed.pending(session, user.org_id, user.id)
    return PendingPage(
        items=[PendingOut.model_validate(row, from_attributes=True) for row in found]
    )


@router.get("/counts")
def my_counts(user: CurrentUser, session: DbSession) -> CountsOut:
    """A rep's own counts: never another rep's, never a ranking."""
    access.only(user, Role.REP)
    found = fixed.my_counts(session, user.org_id, user.id)
    return CountsOut(raised=found.raised, fixed=found.fixed)


class OverviewOut(BaseModel):
    """The home's counts for whoever works issues: a team member's team's issues, or a
    manager's or admin's whole org (overview.py says what each counts)."""

    open: int
    in_progress: int
    # Open or in progress, and past their due date.
    overdue: int
    # Since this IST week's Monday 00:00.
    resolved_this_week: int
    # The whole org's only (a manager's or admin's): None for a team member.
    triage: int | None
    visits_this_week: int | None
    # An admin's only: the catalog screen's names waiting for review.
    names_to_review: int | None


@router.get("/overview")
def my_overview(user: CurrentUser, session: DbSession) -> OverviewOut:
    """For a team member, manager or admin; a rep's is 404."""
    access.team_work(user)
    return OverviewOut.model_validate(asdict(overview.overview(session, user)))


class RepCountsOut(BaseModel):
    """A field sales rep's card on Triage (overview.py says what each counts)."""

    rep_id: str
    rep_name: str
    confirmed: int
    in_review: int
    # Issues from their visits, open or in progress now.
    pending: int


@router.get("/reps")
def my_reps(user: CurrentUser, session: DbSession) -> list[RepCountsOut]:
    """The org's reps, by name, for a manager or admin, as Triage is; anyone else's is 404."""
    access.only(user, *access.WHOLE_ORG)
    found = overview.rep_counts(session, user.org_id)
    return [RepCountsOut.model_validate(asdict(rep)) for rep in found]
