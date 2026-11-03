"""Gives every shop that hasn't answered a paper consent yes: a dev tool (batch 17).

    uv run python scripts/consent_shops.py [--yes]

The user, 3 Nov: every shop already in the data has consented, by link or on paper. So each shop
in each org with no answer gets a paper yes, marked by one of that org's own reps (or, if it has
none, a manager, then an admin). A shop with an answer, yes or no, is left as it is, so it's safe
to run again. Without --yes it changes nothing and prints what it would do.
"""

import argparse
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk import consent
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.logs import configure_logging
from countertalk.models import Org, Role, Shop, User

# Who marks the forms, in the order asked for.
MARKERS = (Role.REP, Role.MANAGER, Role.ADMIN)
# The language of the forms: not known for these, so Hindi, the demo's first.
LANGUAGE = "hi"


def marker(session: Session, org_id: str) -> User | None:
    """The org's first rep, by when they were added; or its first manager, then admin."""
    for role in MARKERS:
        found = session.scalars(
            select(User)
            .where(User.org_id == org_id, User.role == role)
            .order_by(User.created_at, User.id)
            .limit(1)
        ).first()
        if found is not None:
            return found
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="consent_shops.py")
    parser.add_argument("--yes", action="store_true", help="mark them; without it, only count")
    args = parser.parse_args(argv)
    left = 0
    try:
        with session_scope() as session:
            orgs = session.execute(select(Org.id, Org.name).order_by(Org.name, Org.id)).all()
            for org_id, name in orgs:
                shops = session.scalars(select(Shop.id).where(Shop.org_id == org_id)).all()
                answered = consent.current(session, org_id, shops)
                waiting = len(shops) - len(answered)
                by = marker(session, org_id)
                if waiting and by is None:
                    left += waiting
                    print(
                        f"{name} ({org_id}): {waiting} without an answer, and no one to mark them"
                    )
                    continue
                marked = waiting
                if args.yes and waiting:
                    assert by is not None
                    marked = consent.mark_unanswered(session, org_id, by.id, LANGUAGE)
                verb = "marked" if args.yes else "to mark"
                who = f", by {by.role} {by.id}" if marked and by is not None else ""
                print(f"{name} ({org_id}): {verb} {marked}{who}; already answered {len(answered)}")
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    if not args.yes:
        print("Nothing was changed. Run again with --yes to mark them.")
    if left:
        print(f"{left} shops are still without an answer", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
