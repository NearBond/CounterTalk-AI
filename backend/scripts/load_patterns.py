"""Loads K6, the planted patterns, into a dev org of its own: a dev tool (batch 15b).

    uv run python scripts/load_patterns.py [--world <world.yaml>]

The world is synthetic, made-up example data (tests/fixtures/patterns/world.yaml): 30 shops,
3 reps and 67 visits over six weeks from Monday 21 Sep 2026, each confirmed the app's way, in
the world's own time (scripts/_k6.py). Its org is named "K6 world (example) …", and it's loaded
once: run again, it says which org has it. It prints the org's ID, for write_summary.py, and
its finance member's example address: give it a password with scripts/set_password.py to
sign in as finance and see it in the app.
"""

import argparse
import sys
from pathlib import Path

import yaml
from sqlalchemy import func, select

from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.logs import configure_logging
from countertalk.models import Org, Visit
from scripts import _k6

NAME = "K6 world (example)"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="load_patterns.py")
    parser.add_argument("--world", type=Path, default=_k6.WORLD, help="the world's YAML file")
    args = parser.parse_args(argv)
    if not args.world.is_file():
        print(f"no world file at {args.world}", file=sys.stderr)
        return 1
    try:
        visits = len(yaml.safe_load(args.world.read_text(encoding="utf-8"))["visits"])
        with session_scope(read_only=True) as session:
            found = session.scalars(select(Org.id).where(Org.name.startswith(NAME))).first()
            there = session.scalar(select(func.count()).where(Visit.org_id == found)) or 0
        if found is not None and there < visits:
            # Each visit is its own transaction: a load stopped part-way leaves the org behind.
            print(
                f"org {found} has {there} of the world's {visits} visits: a load stopped"
                " part-way, so load it into a fresh database",
                file=sys.stderr,
            )
            return 1
        if found is not None:
            print(f"loaded already: org {found}")
        else:
            world = _k6.load(args.world)
            found = world.org_id
            # One insight a visit; the shops map holds K6's letters too, so they're counted by ID.
            made = sum(len(ids) for ids in world.insights.values())
            print(f"loaded: org {found}, {len(set(world.shops.values()))} shops, {made} visits")
        print(
            f"to see it in the app, sign in as {_k6.finance_email(found)}"
            " (scripts/set_password.py gives it a password)"
        )
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
