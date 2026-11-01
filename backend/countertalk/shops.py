"""Shops: the org's shops, for picking one before a visit."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk.models import Shop


def list_shops(session: Session, org_id: str) -> list[Shop]:
    return list(
        session.scalars(select(Shop).where(Shop.org_id == org_id).order_by(Shop.name, Shop.area))
    )
