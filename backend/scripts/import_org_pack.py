"""Loads an org pack's teams, distributors and catalog into an org: a dev tool (batch 8).

    uv run python scripts/import_org_pack.py "<org name>" <org_pack_id> <version>
        [--root <folder>] [--playbooks <folder>]

It puts the org on the pack and only adds what's missing, keyed by (org, key). It never changes or
deletes a row, so edits made on the catalog screen survive, and running it again adds nothing.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import select

from countertalk.catalog import import_org_pack
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.logs import configure_logging
from countertalk.models import Org
from countertalk.playbooks import ORG_PACKS_DIR, PLAYBOOKS_DIR, load_org_pack


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="import_org_pack.py")
    parser.add_argument("org_name")
    parser.add_argument("pack_id")
    parser.add_argument("version", type=int)
    parser.add_argument("--root", type=Path, default=ORG_PACKS_DIR, help="the org packs' folder")
    parser.add_argument(
        "--playbooks", type=Path, default=PLAYBOOKS_DIR, help="the playbooks' folder"
    )
    args = parser.parse_args(argv)
    try:
        pack = load_org_pack(args.pack_id, args.version, args.root, args.playbooks)
        with session_scope() as session:
            org_id = session.scalars(select(Org.id).where(Org.name == args.org_name)).one_or_none()
            if org_id is None:
                print(f"no org is named {args.org_name!r}", file=sys.stderr)
                return 1
            imported = import_org_pack(session, org_id, pack)
    except SETUP_ERRORS as error:
        print(f"can't import: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:  # a pack that doesn't check out, or an org on another pack
        print(f"can't import: {describe(error)}", file=sys.stderr)
        return 1
    print(
        f"{args.org_name} is on {pack.id} v{pack.version}: added {imported.teams} teams,"
        f" {imported.distributors} distributors, {imported.items} catalog items and"
        f" {imported.aliases} aliases"
    )
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
