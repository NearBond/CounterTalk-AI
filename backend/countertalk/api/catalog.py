"""/api/catalog (batch 16b): the catalog screen's items, aliases and review queue. Admins only;
the rules are catalog_admin's."""

from dataclasses import asdict
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from countertalk import access, catalog_admin
from countertalk.api.deps import CurrentUser
from countertalk.db import DbSession
from countertalk.models import CatalogKind, Role

router = APIRouter(prefix="/api/catalog", tags=["catalog"])

NAME = Field(min_length=1, max_length=200)


class AliasOut(BaseModel):
    id: str
    text: str
    source: str


class ItemOut(BaseModel):
    id: str
    key: str
    kind: str
    company: str
    brand: str
    # None for a brand as a whole.
    product: str | None
    active: bool
    # What an edit names (compare-and-set): 409 item_changed if it moved on.
    version: int
    aliases: list[AliasOut]


class ItemPage(BaseModel):
    items: list[ItemOut]
    # The list shape (§2.11): one page for now.
    next_cursor: str | None = None


class NewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str = NAME
    brand: str = NAME
    product: str | None = Field(default=None, max_length=200)


class ItemIn(NewItem):
    kind: CatalogKind


class ItemChange(BaseModel):
    """The version read, and any fields to change; `product: null` makes it the brand's item."""

    model_config = ConfigDict(extra="forbid")

    version: StrictInt
    company: str | None = Field(default=None, min_length=1, max_length=200)
    brand: str | None = Field(default=None, min_length=1, max_length=200)
    product: str | None = Field(default=None, max_length=200)
    active: StrictBool | None = None


class AliasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = NAME


class ReviewOut(BaseModel):
    """A name linking couldn't place, and the visits it was said in."""

    id: str
    kind: str
    text: str
    visit_ids: list[str]


class ResolveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["attach", "create", "dismiss"]
    # attach: the item it's another name of.
    item_id: UUID | None = None
    # create: the new item (its kind is the name's).
    item: NewItem | None = None


class Resolved(BaseModel):
    # The item it's now an alias of; None when dismissed, or nothing changed.
    item: ItemOut | None


def _out(found: catalog_admin.ItemView) -> ItemOut:
    return ItemOut.model_validate(asdict(found))


@router.get("/items")
def list_items(
    user: CurrentUser,
    session: DbSession,
    q: str | None = None,
    kind: CatalogKind | None = None,
) -> ItemPage:
    """Active and deactivated items; `q` matches a company, brand, product or alias."""
    access.only(user, Role.ADMIN)
    found = catalog_admin.items(session, user.org_id, q, kind)
    return ItemPage(items=[_out(view) for view in found])


@router.post("/items", status_code=status.HTTP_201_CREATED)
def add_item(body: ItemIn, user: CurrentUser, session: DbSession) -> ItemOut:
    """409 item_exists, or name_in_use for a name another item is matched on."""
    access.only(user, Role.ADMIN)
    found = catalog_admin.create_item(
        session, user.org_id, user.id, body.kind, body.company, body.brand, body.product
    )
    return _out(catalog_admin.item(session, user.org_id, found.id))


@router.patch("/items/{item_id}")
def change_item(item_id: UUID, body: ItemChange, user: CurrentUser, session: DbSession) -> ItemOut:
    """Deactivating is `active: false`: never a delete. 409 item_changed on an old version."""
    access.only(user, Role.ADMIN)
    changes = {name: getattr(body, name) for name in body.model_fields_set if name != "version"}
    catalog_admin.change_item(session, user.org_id, user.id, str(item_id), body.version, **changes)
    return _out(catalog_admin.item(session, user.org_id, str(item_id)))


@router.post("/items/{item_id}/aliases", status_code=status.HTTP_201_CREATED)
def add_alias(item_id: UUID, body: AliasIn, user: CurrentUser, session: DbSession) -> ItemOut:
    """The item with its new alias (the same again is the one there). 400 alias_empty; 409
    alias_in_use for a name another item is matched on."""
    access.only(user, Role.ADMIN)
    catalog_admin.add_alias(session, user.org_id, user.id, str(item_id), body.text)
    return _out(catalog_admin.item(session, user.org_id, str(item_id)))


@router.delete("/aliases/{alias_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_alias(alias_id: UUID, user: CurrentUser, session: DbSession) -> Response:
    access.only(user, Role.ADMIN)
    catalog_admin.remove_alias(session, user.org_id, user.id, str(alias_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/reviews")
def list_reviews(user: CurrentUser, session: DbSession) -> list[ReviewOut]:
    """The open names, one per kind and name, the most said first."""
    access.only(user, Role.ADMIN)
    return [
        ReviewOut.model_validate(asdict(v))
        for v in catalog_admin.open_reviews(session, user.org_id)
    ]


@router.post("/reviews/{review_id}/resolve")
def resolve_review(
    review_id: UUID, body: ResolveIn, user: CurrentUser, session: DbSession
) -> Resolved:
    """Attach the name to an item, create one from it, or dismiss it: every open review of the
    name. The same again changes nothing; 409 review_resolved for another way."""
    access.only(user, Role.ADMIN)
    new_item = body.item.model_dump() if body.item is not None else None
    target = catalog_admin.resolve(
        session,
        user.org_id,
        user.id,
        str(review_id),
        body.action,
        str(body.item_id) if body.item_id is not None else None,
        new_item,
    )
    if target is None:
        return Resolved(item=None)
    return Resolved(item=_out(catalog_admin.item(session, user.org_id, target.id)))
