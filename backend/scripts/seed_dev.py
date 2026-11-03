"""Seeds the demo org, its rep and a few shops: `uv run python scripts/seed_dev.py`.

    uv run python scripts/seed_dev.py [--org demo|maharashtra]

The demo org runs on the PepsiCo India example pack (batch 8): its teams, distributors and
catalog are imported, and the shops are spread across the distributors and their beats.
`--org maharashtra` seeds a second org on the Maharashtra example pack instead (batch T3), for
visits in Marathi: two shops and no rep, whose reps are added with add_user.py.

Safe to run again, even twice at once: rows are matched on their natural keys, and existing
ones are left exactly as they are. A shop's distributor, and its beat, are each filled in only
while it has none. Every name is made up, and the rep's address is example.com.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk.catalog import import_org_pack
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import Conflict, DomainError, describe
from countertalk.logs import configure_logging
from countertalk.models import Base, Distributor, Org, Role, Shop, User
from countertalk.playbooks import OrgPack, load_org_pack

DEMO_ORG_NAME = "Demo Org"
# The dev org's rep: scripts/set_password.py gives them a password to sign in with.
DEMO_REP = {"email": "rep@example.com", "name": "Demo Rep"}
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
class DemoOrg:
    name: str
    pack: tuple[str, int]
    # Its rep, made with it (None: its reps come from add_user.py, with a password).
    rep: dict[str, str] | None
    shops: list[tuple[str, str, str, str]]


DEMO = DemoOrg(DEMO_ORG_NAME, DEMO_PACK, DEMO_REP, DEMO_SHOPS)
# Batch T3's org for the Marathi role-plays: fictional shops in the pack's Pune and Nashik.
MAHARASHTRA = DemoOrg(
    "Demo Org Maharashtra",
    ("pepsico-maharashtra-demo", 1),
    None,
    [
        ("Morning Star Kirana", "Pune", "d1", "Pune Monday"),
        ("Grapevine General Store", "Nashik", "d3", "Nashik Wednesday"),
    ],
)
ORGS = {"demo": DEMO, "maharashtra": MAHARASHTRA}


@dataclass(frozen=True)
class SeedResult:
    org_id: str
    created: int
    existing: int


def seed(session: Session, pack: OrgPack, demo: DemoOrg = DEMO) -> SeedResult:
    """A demo org, its rep if it has one, and its shops, on `pack` (load it before the
    transaction)."""
    inserted = [_insert_missing(session, Org, {"name": demo.name}, ["name"])]
    org_id = session.scalars(select(Org.id).where(Org.name == demo.name)).one()
    if demo.rep is not None:
        rep = {"org_id": org_id, "role": Role.REP.value, **demo.rep}
        inserted.append(_insert_missing(session, User, rep, ["email"]))
        email = demo.rep["email"]
        rep_org_id = session.scalars(select(User.org_id).where(User.email == email)).one()
        if rep_org_id != org_id:
            # Emails are unique across orgs, so the demo org would silently have no rep.
            raise Conflict("demo_rep_in_another_org", f"{email} is in another org")
    for name, area, _, _ in demo.shops:
        shop = {"org_id": org_id, "name": name, "area": area}
        inserted.append(_insert_missing(session, Shop, shop, ["org_id", "name", "area"]))
    import_org_pack(session, org_id, pack)
    distributors = dict(
        session.execute(
            select(Distributor.key, Distributor.id).where(Distributor.org_id == org_id)
        ).all()
    )
    for name, area, distributor, beat in demo.shops:
        this_shop = (Shop.org_id == org_id, Shop.name == name, Shop.area == area)
        session.execute(
            update(Shop)
            .where(*this_shop, Shop.distributor_id.is_(None))
            .values(distributor_id=distributors[distributor])
        )
        session.execute(update(Shop).where(*this_shop, Shop.beat.is_(None)).values(beat=beat))
    return SeedResult(org_id, created=sum(inserted), existing=len(inserted) - sum(inserted))


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


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(prog="seed_dev.py")
    parser.add_argument("--org", choices=list(ORGS), default="demo")
    demo = ORGS[parser.parse_args(list(argv)).org]
    try:
        pack = load_org_pack(*demo.pack)  # before the transaction: no lock waits on file reads
        with session_scope() as session:
            result = seed(session, pack, demo)
    except SETUP_ERRORS as error:
        print(f"can't seed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:
        print(f"can't seed: {describe(error)}", file=sys.stderr)
        return 1
    # The org's ID, for add_user.py.
    print(
        f"seeded {demo.name} ({result.org_id}): created {result.created},"
        f" already there {result.existing}"
    )
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
