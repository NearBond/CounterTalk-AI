"""Checks an org pack and its playbook, and prints the routing table: a dev tool (batch 8).

    uv run python scripts/check_pack.py <org_pack_id> <version> [--schema] [--root <folder>]
        [--playbooks <folder>]

Every problem is one line naming the file and the field. --schema prints the extraction schema
for the pack's org instead. --root and --playbooks load packs and playbooks from other
folders (copies being edited).
"""

import argparse
import json
import sys
from pathlib import Path

from countertalk.logs import configure_logging
from countertalk.playbooks import (
    ORG_PACKS_DIR,
    PLAYBOOKS_DIR,
    PackError,
    build_extraction_schema,
    load_org_pack,
    routing_table,
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="check_pack.py")
    parser.add_argument("pack_id")
    parser.add_argument("version", type=int)
    parser.add_argument("--schema", action="store_true", help="print the extraction schema")
    parser.add_argument("--root", type=Path, default=ORG_PACKS_DIR, help="the org packs' folder")
    parser.add_argument(
        "--playbooks", type=Path, default=PLAYBOOKS_DIR, help="the playbooks' folder"
    )
    args = parser.parse_args(argv)
    try:
        pack = load_org_pack(args.pack_id, args.version, args.root, args.playbooks)
    except PackError as error:
        print(f"invalid: {error.message}", file=sys.stderr)
        return 1
    if args.schema:
        print(
            json.dumps(build_extraction_schema(pack.playbook, pack), indent=2, ensure_ascii=False)
        )
        return 0
    print(f"{pack.id} v{pack.version}: {pack.file.display_name}")
    playbook = pack.playbook
    print(f"playbook: {playbook.id} v{playbook.version} ({len(playbook.categories)} categories)")
    print(f"teams: {', '.join(pack.file.teams)}")
    print(
        f"catalog: {len(pack.skus)} own and {len(pack.competitors)} competitor rows,"
        f" {len(pack.schemes)} schemes, {len(pack.distributors)} distributors"
    )
    print("routing:")
    table = routing_table(pack)
    for route in table:
        also = (
            f"; {route.override.min_severity} and above also {route.override.team}"
            if route.override
            else ""
        )
        print(f"  {route.category:<11} {route.team or '(no team: triage)'}{also}")
    unrouted = [route.category for route in table if route.team is None]
    print(f"no team, so triage: {', '.join(unrouted) or 'none'}")
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # Devanagari aliases
    sys.exit(main(sys.argv[1:]))
