"""Seeds the demo org, its rep and a few shops: `uv run python scripts/seed_dev.py`.

The demo org runs on the PepsiCo India example pack (batch 8): its teams, distributors and
catalog are imported, and the shops are spread across the distributors and their beats.

Safe to run again, even twice at once: rows are matched on their natural keys, and existing
ones are left exactly as they are. A shop's distributor, and its beat, are each filled in only
while it has none. Every name is made up, and the rep's address is example.com.
"""

import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk.catalog import import_org_pack
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import Conflict, DomainError, describe
from countertalk.models import Base, Distributor, Org, Role, Shop, User
from countertalk.playbooks import OrgPack, load_org_pack
from countertalk.users import DEMO_REP_EMAIL

DEMO_ORG_NAME = "Demo Org"
# The app treats this rep as the current user until batch 16 (countertalk/users.py).
DEMO_REP = {"email": DEMO_REP_EMAIL, "name": "Demo Rep"}
DEMO_PACK = ("pepsico-india-demo", 1)
# (name, area, distributor key, beat): fictional kirana shops in made-up areas, on the pack's
# made-up distributors.
DEMO_SHOPS = [
    ("Sunrise Kirana", "Old Market", "d1", "Old Market Monday"),
    ("Lotus General Store", "Old Market", "d2", "Old Market Thursday"),
    ("Corner Daily Needs", "Station Road", "d3", "Station Road Tuesday"),
    ("Moonlight Provisions", "Riverside", "d3", "Riverside Friday"),
]


@dataclass(frozen=True)
class SeedResult:
    created: int
    existing: int


def seed(session: Session, pack: OrgPack) -> SeedResult:
    """The demo org, its rep and shops, on `pack` (load it before the transaction)."""
    inserted = [_insert_missing(session, Org, {"name": DEMO_ORG_NAME}, ["name"])]
    org_id = session.scalars(select(Org.id).where(Org.name == DEMO_ORG_NAME)).one()
    rep = {"org_id": org_id, "role": Role.REP.value, **DEMO_REP}
    inserted.append(_insert_missing(session, User, rep, ["email"]))
    rep_org_id = session.scalars(select(User.org_id).where(User.email == DEMO_REP["email"])).one()
    if rep_org_id != org_id:
        # Emails are unique across orgs, so the demo org would silently have no rep.
        raise Conflict("demo_rep_in_another_org", f"{DEMO_REP['email']} is in another org")
    for name, area, _, _ in DEMO_SHOPS:
        shop = {"org_id": org_id, "name": name, "area": area}
        inserted.append(_insert_missing(session, Shop, shop, ["org_id", "name", "area"]))
    import_org_pack(session, org_id, pack)
    distributors = dict(
        session.execute(
            select(Distributor.key, Distributor.id).where(Distributor.org_id == org_id)
        ).all()
    )
    for name, area, distributor, beat in DEMO_SHOPS:
        this_shop = (Shop.org_id == org_id, Shop.name == name, Shop.area == area)
        session.execute(
            update(Shop)
            .where(*this_shop, Shop.distributor_id.is_(None))
            .values(distributor_id=distributors[distributor])
        )
        session.execute(update(Shop).where(*this_shop, Shop.beat.is_(None)).values(beat=beat))
    return SeedResult(created=sum(inserted), existing=len(inserted) - sum(inserted))


def _insert_missing(
    session: Session, model: type[Base], values: dict[str, Any], natural_key: list[str]
) -> int:
    """Inserts the row unless its natural key is taken. Returns 1 if it inserted."""
    table = model.__table__
    statement = (
        insert(table)
        .values(**values)
        .on_conflict_do_nothing(index_elements=natural_key)
        # Only a row actually inserted comes back.
        .returning(table.c.id)
    )
    return len(session.execute(statement).all())


def main() -> int:
    try:
        pack = load_org_pack(*DEMO_PACK)  # before the transaction: no lock waits on file reads
        with session_scope() as session:
            result = seed(session, pack)
    except SETUP_ERRORS as error:
        print(f"can't seed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:
        print(f"can't seed: {describe(error)}", file=sys.stderr)
        return 1
    print(f"seeded: created {result.created}, already there {result.existing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
