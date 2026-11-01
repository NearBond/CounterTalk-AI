"""Seeds the demo org, its rep and a few shops: `uv run python scripts/seed_dev.py`.

Safe to run again, even twice at once: rows are matched on their natural keys, and existing
ones are left exactly as they are. Every name is made up, and the rep's address is example.com.
"""

import sys
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import ArgumentError, DBAPIError, OperationalError
from sqlalchemy.orm import Session

from countertalk.db import session_scope
from countertalk.errors import Conflict, DomainError, describe
from countertalk.models import Base, Org, Role, Shop, User
from countertalk.users import DEMO_REP_EMAIL

DEMO_ORG_NAME = "Demo Org"
# The app treats this rep as the current user until batch 16 (countertalk/users.py).
DEMO_REP = {"email": DEMO_REP_EMAIL, "name": "Demo Rep"}
# (name, area): fictional kirana shops in made-up areas.
DEMO_SHOPS = [
    ("Sunrise Kirana", "Old Market"),
    ("Lotus General Store", "Old Market"),
    ("Corner Daily Needs", "Station Road"),
    ("Moonlight Provisions", "Riverside"),
]


@dataclass(frozen=True)
class SeedResult:
    created: int
    existing: int


def seed(session: Session) -> SeedResult:
    inserted = [_insert_missing(session, Org, {"name": DEMO_ORG_NAME}, ["name"])]
    org_id = session.scalars(select(Org.id).where(Org.name == DEMO_ORG_NAME)).one()
    rep = {"org_id": org_id, "role": Role.REP.value, **DEMO_REP}
    inserted.append(_insert_missing(session, User, rep, ["email"]))
    rep_org_id = session.scalars(select(User.org_id).where(User.email == DEMO_REP["email"])).one()
    if rep_org_id != org_id:
        # Emails are unique across orgs, so the demo org would silently have no rep.
        raise Conflict("demo_rep_in_another_org", f"{DEMO_REP['email']} is in another org")
    for name, area in DEMO_SHOPS:
        shop = {"org_id": org_id, "name": name, "area": area}
        inserted.append(_insert_missing(session, Shop, shop, ["org_id", "name", "area"]))
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
        with session_scope() as session:
            result = seed(session)
    except (ValidationError, ArgumentError):
        print("can't seed: the settings aren't valid; run scripts/check_setup.py", file=sys.stderr)
        return 1
    except OperationalError as error:
        # One line, and never the driver's text, which can quote the URL: check_setup.py says why.
        print(
            f"can't seed: the database didn't answer ({type(error.orig).__name__});"
            " run scripts/check_setup.py",
            file=sys.stderr,
        )
        return 1
    except DBAPIError as error:
        # It answered, and refused: most often a database that isn't migrated yet.
        print(
            f"can't seed: the database refused it ({type(error.orig).__name__});"
            " is it migrated? run uv run alembic upgrade head",
            file=sys.stderr,
        )
        return 1
    except DomainError as error:
        print(f"can't seed: {describe(error)}", file=sys.stderr)
        return 1
    print(f"seeded: created {result.created}, already there {result.existing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
