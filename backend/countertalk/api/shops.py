"""/api/shops: the org's shops, for picking one before a visit, with each one's consent to
recording; and for a shop without a yes, a consent link to share or the paper form marked
(batch 17)."""

from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from countertalk import access, consent, shops
from countertalk.api.deps import CurrentUser
from countertalk.db import DbSession
from countertalk.models import ConsentDecision, ConsentLanguage, Role

router = APIRouter(prefix="/api/shops", tags=["shops"])
# Who shares a link or marks a paper form: the org's own staff, not a team's.
CONSENT_TAKERS = (Role.REP, Role.MANAGER, Role.ADMIN)


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


class ConsentLinkOut(BaseModel):
    # The shop's consent page, with its token: the app makes the full link from its own origin.
    path: str


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
    access.only(user, *CONSENT_TAKERS)
    token = consent.new_link(session, user.org_id, str(shop_id), user.id)
    return ConsentLinkOut(path=f"/consent/{token}")


@router.post("/{shop_id}/consent", status_code=status.HTTP_201_CREATED)
def mark_paper_consent(
    shop_id: UUID, body: PaperIn, user: CurrentUser, session: DbSession
) -> ConsentOut:
    """The shopkeeper signed the paper form with the invoice: a yes, saved with this user."""
    access.only(user, *CONSENT_TAKERS)
    return ConsentOut(
        consent=consent.mark_paper(session, user.org_id, str(shop_id), user.id, body.language)
    )
