"""The catalog screen (batch 16b): an admin keeps the org's own products and its competitors' up to
date in the app, instead of in the org pack's files.

- **Items:** added with a kind, company, brand and product (none for a brand as a whole), and
  edited with compare-and-set on their version (409 item_changed, nothing written). Deactivated,
  never deleted, as insights point at them: linking (entities.load_catalog) and the transcription
  keywords (catalog.keywords_for) read active items only. A new item's key is "admin-…", so a
  re-import of the pack, which adds only missing keys and never changes a row, keeps every edit.
- **Names:** compared by linking's own key (entities.link_key). An alias that's empty once
  normalised is refused (400 alias_empty). Any change (an alias, a new item, a rename) is
  checked on the whole catalog as linking would read it after, active items or not (one may
  come back): a name of the item's, or of its brand's or products' ("brand product" with every
  brand alias), that another item is matched on too would be ambiguous (409 alias_in_use, or
  name_in_use for an item's own name). An item and its own brand or products don't clash:
  linking prefers the product. The same alias on its item again is the one there.
- **The review queue:** the names linking couldn't place (AliasReview), one per kind and name
  however many visits said it. Attach it to an item as an alias, create an item from it, or
  dismiss it; each resolves every open review of that name. The same again changes nothing;
  another way, on a resolved one, is 409 review_resolved.
- **The log:** every change is an Edit row with no visit, append-only.

The org's catalog changes one at a time (its org row locked), so two admins can't both add a
name that's free on its own (R6). New visits use a change at once; past insights keep their links.
"""

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import JSON, select
from sqlalchemy.orm import Session

from countertalk import entities, rows
from countertalk.errors import Conflict, Invalid, NotFound
from countertalk.models import (
    CREATED,
    DELETED,
    AliasReview,
    AliasReviewStatus,
    AliasSource,
    CatalogAlias,
    CatalogItem,
    CatalogKind,
    Edit,
    EditObject,
    Org,
    new_id,
)

NAMES = ("company", "brand", "product")
# How a resolve leaves its reviews, by action.
RESOLVED = {
    "attach": AliasReviewStatus.ATTACHED,
    "create": AliasReviewStatus.CREATED,
    "dismiss": AliasReviewStatus.DISMISSED,
}


def missing_item() -> NotFound:
    return NotFound("item_not_found", "There's no such catalog item")


def missing_alias() -> NotFound:
    return NotFound("alias_not_found", "There's no such alias")


def missing_review() -> NotFound:
    return NotFound("review_not_found", "There's no such name to review")


@dataclass(frozen=True)
class AliasView:
    id: str
    text: str
    source: str


@dataclass(frozen=True)
class ItemView:
    id: str
    key: str
    kind: str
    company: str
    brand: str
    product: str | None
    active: bool
    version: int
    aliases: list[AliasView]


@dataclass(frozen=True)
class ReviewView:
    """An open name, said in these visits (oldest first): `id` is the first review's."""

    id: str
    kind: str
    text: str
    visit_ids: list[str]


# --- reading ---------------------------------------------------------------------------------


def items(
    session: Session, org_id: str, q: str | None = None, kind: str | None = None
) -> list[ItemView]:
    """The org's items, active or not, own first, by brand and product; `q` matches a company,
    brand, product or alias, in any case."""
    aliases: dict[str, list[AliasView]] = defaultdict(list)
    for alias in session.scalars(
        select(CatalogAlias).where(CatalogAlias.org_id == org_id).order_by(CatalogAlias.created_at)
    ):
        aliases[alias.item_id].append(AliasView(alias.id, alias.text, alias.source))
    query = select(CatalogItem).where(CatalogItem.org_id == org_id)
    if kind is not None:
        query = query.where(CatalogItem.kind == kind)
    found = [
        _view(item, aliases[item.id])
        for item in session.scalars(
            query.order_by(
                CatalogItem.kind != CatalogKind.OWN, CatalogItem.brand, CatalogItem.product
            )
        )
    ]
    wanted = (q or "").strip().casefold()
    if not wanted:
        return found
    return [view for view in found if any(wanted in text.casefold() for text in _texts(view))]


def item(session: Session, org_id: str, item_id: str) -> ItemView:
    found = rows.find_one(session, CatalogItem, org_id, item_id, missing_item())
    return _view(found, [AliasView(a.id, a.text, a.source) for a in _aliases_of(session, found.id)])


def open_reviews(session: Session, org_id: str) -> list[ReviewView]:
    """One per kind and name, the most said first, then the oldest."""
    grouped: dict[tuple[str, str], list[AliasReview]] = defaultdict(list)
    for review in session.scalars(
        select(AliasReview)
        .where(AliasReview.org_id == org_id, AliasReview.status == AliasReviewStatus.OPEN)
        .order_by(AliasReview.created_at, AliasReview.id)
    ):
        grouped[(review.kind, review.normalised)].append(review)
    views = [
        ReviewView(found[0].id, found[0].kind, found[0].text, [r.visit_id for r in found])
        for found in grouped.values()
    ]
    return sorted(views, key=lambda view: -len(view.visit_ids))


def _view(found: CatalogItem, aliases: list[AliasView]) -> ItemView:
    return ItemView(
        found.id,
        found.key,
        found.kind,
        found.company,
        found.brand,
        found.product,
        found.active,
        found.version,
        aliases,
    )


def _texts(view: ItemView) -> list[str]:
    return [view.company, view.brand, view.product or "", *(a.text for a in view.aliases)]


def _aliases_of(session: Session, item_id: str) -> list[CatalogAlias]:
    return list(
        session.scalars(
            select(CatalogAlias)
            .where(CatalogAlias.item_id == item_id)
            .order_by(CatalogAlias.created_at)
        )
    )


# --- items -----------------------------------------------------------------------------------


def create_item(
    session: Session,
    org_id: str,
    actor: str,
    kind: str,
    company: str,
    brand: str,
    product: str | None = None,
) -> CatalogItem:
    """A new item: 409 item_exists for a kind, brand and product the org has, 409 name_in_use
    for a name another item is matched on."""
    _lock_catalog(session, org_id)
    if kind not in set(CatalogKind):
        raise Invalid("unknown_kind", "An item is the org's own or a competitor's")
    fields = _fields(company=company, brand=brand, product=product)
    found = CatalogItem(
        id=new_id(), org_id=org_id, key=f"admin-{new_id()[:8]}", kind=kind, **fields
    )
    _not_twice(session, found)
    session.add(found)
    _unambiguous(session, found, "name_in_use")
    _log(
        session,
        org_id,
        actor,
        EditObject.CATALOG_ITEM,
        found.id,
        CREATED,
        None,
        {"kind": kind, **fields},
    )
    return found


def change_item(
    session: Session, org_id: str, actor: str, item_id: str, version: int, **changes: Any
) -> CatalogItem:
    """The fields given (NAMES, active) on the version read: 409 item_changed if it moved on."""
    _lock_catalog(session, org_id)
    found = rows.find_one(session, CatalogItem, org_id, item_id, missing_item(), for_update=True)
    if found.version != version:
        raise Conflict("item_changed", "Someone changed this item since you opened it: reload it")
    named = {name: changes.get(name, getattr(found, name)) for name in NAMES}
    wanted: dict[str, Any] = _fields(**named)
    if "active" in changes:
        if not isinstance(changes["active"], bool):
            raise Invalid("active_needed", "Active is true or false")
        wanted["active"] = changes["active"]
    changed = {field: value for field, value in wanted.items() if getattr(found, field) != value}
    if not changed:
        return found
    before = {field: getattr(found, field) for field in changed}
    for field, value in changed.items():
        setattr(found, field, value)
    if changed.keys() & set(NAMES):
        _not_twice(session, found)
        _unambiguous(session, found, "name_in_use")
    found.version += 1
    for field, value in changed.items():
        _log(session, org_id, actor, EditObject.CATALOG_ITEM, found.id, field, before[field], value)
    session.flush()
    return found


def _fields(company: str | None, brand: str | None, product: str | None) -> dict[str, Any]:
    company, brand = (company or "").strip(), (brand or "").strip()
    product = (product or "").strip() or None
    if not company or not brand:
        raise Invalid("name_needed", "An item needs its company and brand")
    return {"company": company, "brand": brand, "product": product}


def _not_twice(session: Session, found: CatalogItem) -> None:
    same = session.scalar(
        select(CatalogItem.id).where(
            CatalogItem.org_id == found.org_id,
            CatalogItem.kind == found.kind,
            CatalogItem.id != found.id,
            CatalogItem.brand == found.brand,
            CatalogItem.product.is_(None)
            if found.product is None
            else CatalogItem.product == found.product,
        )
    )
    if same is not None:
        raise Conflict("item_exists", "The catalog has that item already", {"item_id": same})


# --- aliases ---------------------------------------------------------------------------------


def add_alias(
    session: Session,
    org_id: str,
    actor: str,
    item_id: str,
    text: str,
    source: str = AliasSource.ADMIN,
) -> CatalogAlias:
    """The alias, added (or the same one there already): 400 alias_empty, 409 alias_in_use."""
    _lock_catalog(session, org_id)
    found = rows.find_one(session, CatalogItem, org_id, item_id, missing_item())
    return _alias(session, actor, found, text, source)


def remove_alias(session: Session, org_id: str, actor: str, alias_id: str) -> None:
    _lock_catalog(session, org_id)
    found = rows.find_one(session, CatalogAlias, org_id, alias_id, missing_alias())
    before = {"item_id": found.item_id, "text": found.text, "source": found.source}
    session.delete(found)
    _log(session, org_id, actor, EditObject.CATALOG_ALIAS, found.id, DELETED, before, None)
    session.flush()


def _alias(
    session: Session, actor: str, found: CatalogItem, text: str, source: str
) -> CatalogAlias:
    text = text.strip()
    key = entities.link_key(text)
    if not key:
        raise Invalid("alias_empty", "That alias is nothing once spaces and marks are taken out")
    same = session.scalars(
        select(CatalogAlias).where(CatalogAlias.item_id == found.id, CatalogAlias.text == text)
    ).one_or_none()
    if same is not None:
        return same
    _unambiguous(session, found, "alias_in_use", text)
    alias = CatalogAlias(org_id=found.org_id, item_id=found.id, text=text, source=source)
    session.add(alias)
    session.flush()
    after = {"item_id": found.id, "text": text, "source": source}
    _log(session, found.org_id, actor, EditObject.CATALOG_ALIAS, alias.id, CREATED, None, after)
    return alias


def _unambiguous(session: Session, found: CatalogItem, code: str, alias: str | None = None) -> None:
    """The catalog, as linking would read it with this change (and `alias` as a new one of
    `found`'s), active items or not: 409 `code` if a name of `found`'s, or of its family's,
    is another item's too."""
    session.flush()
    listed = entities.entries(session, found.org_id, active_only=False)
    if alias is not None:
        listed = [
            replace(entry, aliases=(*entry.aliases, alias)) if entry.id == found.id else entry
            for entry in listed
        ]
    names = entities.catalog_of(listed).names
    affected = {found.id, *_family(session, found)}
    for key in sorted(names.by_key):
        targets = names.by_key[key]
        others = sorted(targets - affected)
        if targets & affected and others:
            raise _in_use(code, session, found.org_id, others[0])


def _family(session: Session, found: CatalogItem) -> list[str]:
    """A product's brand item, or a brand's products: the same company, brand and kind."""
    same = select(CatalogItem.id).where(
        CatalogItem.org_id == found.org_id,
        CatalogItem.kind == found.kind,
        CatalogItem.company == found.company,
        CatalogItem.brand == found.brand,
        CatalogItem.id != found.id,
    )
    wanted = (
        CatalogItem.product.is_(None)
        if found.product is not None
        else CatalogItem.product.is_not(None)
    )
    return list(session.scalars(same.where(wanted)))


def _in_use(code: str, session: Session, org_id: str, item_id: str) -> Conflict:
    other = rows.find_one(session, CatalogItem, org_id, item_id, missing_item())
    name = entities.item_name(other.brand, other.product)
    return Conflict(code, f"{name} is matched on that name already", {"item_id": item_id})


# --- the review queue ------------------------------------------------------------------------


def resolve(
    session: Session,
    org_id: str,
    actor: str,
    review_id: str,
    action: str,
    item_id: str | None = None,
    new_item: dict[str, Any] | None = None,
) -> CatalogItem | None:
    """Resolves the name, every open review of it: the item it's now an alias of, or None
    (dismissed). The same again changes nothing; another way is 409 review_resolved."""
    _lock_catalog(session, org_id)
    found = rows.find_one(
        session, AliasReview, org_id, review_id, missing_review(), for_update=True
    )
    status = RESOLVED[action]
    if found.status != AliasReviewStatus.OPEN:
        if found.status != status or not _same_again(session, found, action, item_id):
            raise Conflict("review_resolved", "That name was resolved another way already")
        return None
    target: CatalogItem | None = None
    if action == "attach":
        if item_id is None:
            raise Invalid("item_needed", "Attach names the item")
        target = rows.find_one(session, CatalogItem, org_id, item_id, missing_item())
        if target.kind != found.kind:
            raise Invalid("kind_differs", "The name was said as the other kind of item")
        if not target.active:
            raise Conflict("item_inactive", "That item is deactivated: linking skips it")
    elif action == "create":
        if new_item is None:
            raise Invalid("item_needed", "Create needs the new item's company and brand")
        target = create_item(session, org_id, actor, found.kind, **new_item)
    if target is not None and not _named_so(target, found.text):
        _alias(session, actor, target, found.text, AliasSource.REVIEW)
    same_name = session.scalars(
        select(AliasReview)
        .where(
            AliasReview.org_id == org_id,
            AliasReview.kind == found.kind,
            AliasReview.normalised == found.normalised,
            AliasReview.status == AliasReviewStatus.OPEN,
        )
        .with_for_update()
    )
    for review in same_name:
        review.status = status
        _log(session, org_id, actor, EditObject.ALIAS_REVIEW, review.id, "status", "open", status)
    session.flush()
    return target


def _same_again(session: Session, found: AliasReview, action: str, item_id: str | None) -> bool:
    """A resolve repeated: an attach to the item the name is an alias of; any create or dismiss."""
    if action != "attach":
        return True
    named = select(CatalogAlias.id).where(
        CatalogAlias.org_id == found.org_id,
        CatalogAlias.item_id == item_id,
        CatalogAlias.text == found.text.strip(),
    )
    return item_id is not None and session.scalar(named) is not None


def _named_so(target: CatalogItem, text: str) -> bool:
    """The name is the item's own already (its brand, or "brand product"): no alias needed."""
    key = entities.link_key(text)
    return key in entities.catalog_of([_entry_of(target)]).names.by_key


def _entry_of(target: CatalogItem) -> entities.Entry:
    return entities.Entry(target.id, target.kind, target.company, target.brand, target.product, ())


# --- shared ----------------------------------------------------------------------------------


def _lock_catalog(session: Session, org_id: str) -> None:
    """The org's catalog changes one at a time: two names free on their own can't both go in."""
    session.execute(select(Org.id).where(Org.id == org_id).with_for_update())


def _log(
    session: Session,
    org_id: str,
    actor: str,
    what: str,
    object_id: str,
    field: str,
    before: Any,
    after: Any,
) -> None:
    """The one way a catalog Edit row is written: no visit (models.CATALOG_OBJECTS). A field that
    was or becomes nothing (a brand's product) is JSON null: SQL NULL means only "created" or
    "deleted" (the log's CHECKs)."""
    session.add(
        Edit(
            org_id=org_id,
            visit_id=None,
            actor_id=actor,
            object=what,
            object_id=object_id,
            field=field,
            before=JSON.NULL if before is None and field != CREATED else before,
            after=JSON.NULL if after is None and field != DELETED else after,
        )
    )
