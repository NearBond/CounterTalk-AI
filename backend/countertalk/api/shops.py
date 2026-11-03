"""/api/shops: the org's shops, for picking one before a visit, with each one's consent to
recording; and for a shop without a yes, a consent link to share or the paper form marked
(batch 17)."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from countertalk import access, consent, fixed, shops
from countertalk.api.deps import CurrentUser
from countertalk.db import DbSession
from countertalk.models import ConsentDecision, ConsentLanguage, Role

router = APIRouter(prefix="/api/shops", tags=["shops"])
# The org's own staff, not a team's: they share a shop's consent link, mark its paper form, and
# see what's been fixed for it.
SHOP_STAFF = (Role.REP, Role.MANAGER, Role.ADMIN)


class ShopOut(BaseModel):
    id: str
    name: str
    area: str
    do_not_record: bool
    # Its current answer; None if it hasn't given one yet.
    consent: ConsentDecision | None


class ShopPage(BaseModel):
    items: list[ShopOut]
    # The list shape (§2.11): one page for now, but the field is there for when it isn't.
    next_cursor: str | None = None


class FixedOut(BaseModel):
    """Fixed since the shop's last visit (batch 17b): never a rep's name or a quote."""

    team: str
    category: str
    # The shop's own insight, as its summary said it.
    problem: str
    # The team's resolution note: the line the rep says.
    note: str
    resolved_at: datetime


class FixedPage(BaseModel):
    items: list[FixedOut]
    next_cursor: str | None = None


class ConsentLinkOut(BaseModel):
    # The shop's consent page, with its token: the app makes the full link from its own origin.
    path: str
    # What the link goes with on WhatsApp: the notice's message, a line per language.
    message: str


class PaperIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The language of the form the shopkeeper signed.
    language: ConsentLanguage


class ConsentOut(BaseModel):
    consent: ConsentDecision


@router.get("")
def list_shops(user: CurrentUser, session: DbSession) -> ShopPage:
    found = shops.list_shops(session, user.org_id)
    answers = consent.current(session, user.org_id, [shop.id for shop in found])
    return ShopPage(
        items=[
            ShopOut(
                id=shop.id,
                name=shop.name,
                area=shop.area,
                do_not_record=shop.do_not_record,
                consent=answers.get(shop.id),
            )
            for shop in found
        ]
    )


@router.post("/{shop_id}/consent-links", status_code=status.HTTP_201_CREATED)
def share_consent_link(shop_id: UUID, user: CurrentUser, session: DbSession) -> ConsentLinkOut:
    """A new link to the shop's consent page; the links shared before stay valid."""
    access.only(user, *SHOP_STAFF)
    token = consent.new_link(session, user.org_id, str(shop_id), user.id)
    return ConsentLinkOut(path=f"/consent/{token}", message=consent.share_message())


@router.post("/{shop_id}/consent", status_code=status.HTTP_201_CREATED)
def mark_paper_consent(
    shop_id: UUID, body: PaperIn, user: CurrentUser, session: DbSession
) -> ConsentOut:
    """The shopkeeper signed the paper form with the invoice: a yes, saved with this user."""
    access.only(user, *SHOP_STAFF)
    return ConsentOut(
        consent=consent.mark_paper(session, user.org_id, str(shop_id), user.id, body.language)
    )


@router.get("/{shop_id}/fixed")
def fixed_since_last_visit(shop_id: UUID, user: CurrentUser, session: DbSession) -> FixedPage:
    """What got fixed for the shop since its last visit before today, newest first (batch 17b)."""
    access.only(user, *SHOP_STAFF)
    found = fixed.since_last_visit(session, user.org_id, str(shop_id))
    return FixedPage(items=[FixedOut.model_validate(item, from_attributes=True) for item in found])
