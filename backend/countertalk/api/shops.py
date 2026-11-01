"""/api/shops: the org's shops, for picking one before a visit."""

from fastapi import APIRouter
from pydantic import BaseModel

from countertalk import shops
from countertalk.api.deps import CurrentUser
from countertalk.db import DbSession

router = APIRouter(prefix="/api/shops", tags=["shops"])


class ShopOut(BaseModel):
    id: str
    name: str
    area: str
    do_not_record: bool


class ShopPage(BaseModel):
    items: list[ShopOut]
    # The list shape (§2.11): one page for now, but the field is there for when it isn't.
    next_cursor: str | None = None


@router.get("")
def list_shops(user: CurrentUser, session: DbSession) -> ShopPage:
    found = shops.list_shops(session, user.org_id)
    return ShopPage(items=[ShopOut.model_validate(shop, from_attributes=True) for shop in found])
