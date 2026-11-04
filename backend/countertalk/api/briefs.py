"""The pre-visit brief (batch F5): every shop's latest and its state, a shop's own, and asking
Muse Spark for a new one (countertalk/brief.py). For reps, managers and admins: a team member
gets 404, as for a page that isn't theirs."""

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from countertalk import access, brief, limits
from countertalk.api.deps import CurrentUser
from countertalk.api.shops import SHOP_STAFF
from countertalk.config import get_settings
from countertalk.db import DbSession
from countertalk.llm.base import StructuredLlm
from countertalk.llm.client import MetaLlm

router = APIRouter(prefix="/api/briefs", tags=["briefs"])
# A shop's own brief, under its shop.
shop_router = APIRouter(prefix="/api/shops", tags=["briefs"])

BriefState = Literal["up_to_date", "out_of_date", "not_enough_visits", "none"]


def brief_llm() -> StructuredLlm | None:
    """Muse Spark, or None without MODEL_API_KEY (503). Tests put a fake in its place."""
    key = get_settings().MODEL_API_KEY.get_secret_value()
    return _brief_client(key) if key else None


@lru_cache(maxsize=1)
def _brief_client(key: str) -> MetaLlm:
    """One client, and its connections, for every brief while the key stays the same."""
    return MetaLlm(
        key, timeout_ms=limits.BRIEF_LLM_TIMEOUT_MS, max_retries=limits.BRIEF_LLM_MAX_RETRIES
    )


BriefLlm = Annotated[StructuredLlm | None, Depends(brief_llm)]


class BriefRowOut(BaseModel):
    """A shop on the briefs page."""

    shop_id: str
    name: str
    area: str
    # Its confirmed visits now.
    visits: int
    state: BriefState
    # Confirmed visits since its latest brief; 0 without one.
    since: int
    # Its latest brief's time; None without one.
    generated_at: datetime | None


class BriefPage(BaseModel):
    # Those with a brief first, the newest first; then those that could have one; then the rest.
    items: list[BriefRowOut]
    next_cursor: str | None = None


class BriefRaisedOut(BaseModel):
    category: str
    problem: str
    # A shopkeeper's words, as said.
    quote: str | None


class BriefOrderedOut(BaseModel):
    name: str
    quantity: float | None
    unit: str | None


class BriefLastVisitOut(BaseModel):
    visited_at: datetime
    raised: list[BriefRaisedOut]
    ordered: list[BriefOrderedOut]


class BriefOpenOut(BaseModel):
    team: str
    status: Literal["open", "in_progress"]
    problem: str
    raised_at: datetime


class BriefFixedOut(BaseModel):
    team: str
    problem: str
    # The resolution note: the line the rep says.
    tell_them: str
    resolved_at: datetime


class BriefOut(BaseModel):
    id: str
    generated_at: datetime
    # The confirmed visits it covers: "after visit N".
    after_visit: int
    # The AI's words, checked.
    overview: list[str]
    last_visit: BriefLastVisitOut
    still_open: list[BriefOpenOut]
    fixed_since: list[BriefFixedOut]
    worth_raising: list[str]


class ShopBriefOut(BaseModel):
    shop_id: str
    name: str
    area: str
    visits: int
    state: BriefState
    since: int
    # Generate (none yet) or Regenerate (a newer confirmed visit) is allowed now.
    can_generate: bool
    brief: BriefOut | None


def _row(shop: brief.Standing) -> BriefRowOut:
    return BriefRowOut(
        shop_id=shop.shop_id,
        name=shop.name,
        area=shop.area,
        visits=shop.visits,
        state=shop.state,  # type: ignore[arg-type]
        since=shop.since,
        generated_at=shop.brief.created_at if shop.brief is not None else None,
    )


def _shop_out(shop: brief.Standing) -> ShopBriefOut:
    saved = shop.brief
    out = None
    if saved is not None:
        content = saved.content
        out = BriefOut(
            id=saved.id,
            generated_at=saved.created_at,
            after_visit=saved.visits,
            overview=content["overview"],
            last_visit=BriefLastVisitOut.model_validate(content["last_visit"]),
            still_open=[BriefOpenOut.model_validate(item) for item in content["still_open"]],
            fixed_since=[BriefFixedOut.model_validate(item) for item in content["fixed_since"]],
            worth_raising=content["worth_raising"],
        )
    return ShopBriefOut(
        **_row(shop).model_dump(exclude={"generated_at"}), can_generate=shop.can_generate, brief=out
    )


@router.get("")
def list_briefs(user: CurrentUser, session: DbSession) -> BriefPage:
    """Every shop of the org, with its latest brief's state."""
    access.only(user, *SHOP_STAFF)
    return BriefPage(items=[_row(shop) for shop in brief.every_shop(session, user.org_id)])


@shop_router.get("/{shop_id}/brief")
def shop_brief(shop_id: UUID, user: CurrentUser, session: DbSession) -> ShopBriefOut:
    """The shop's latest brief, read with no AI call."""
    access.only(user, *SHOP_STAFF)
    return _shop_out(brief.of_shop(session, user.org_id, str(shop_id)))


@shop_router.post("/{shop_id}/brief")
def generate_brief(shop_id: UUID, user: CurrentUser, llm: BriefLlm) -> ShopBriefOut:
    """A new brief, written by Muse Spark from the shop's facts. 409 brief_not_enough_visits or
    brief_up_to_date when it isn't allowed now; 503 llm_not_configured without a key."""
    access.only(user, *SHOP_STAFF)
    return _shop_out(brief.generate(llm, user, str(shop_id)))
