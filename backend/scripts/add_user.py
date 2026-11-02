"""Adds a made-up user to an org, to sign in as (batch 16): a dev and test tool.

    NEW_PASSWORD=<password> uv run python scripts/add_user.py <org_id> <email> <name> <role> \\
        [--team <key>]

The role is rep, team_member, manager or admin; a team member needs --team (its team's key),
and only a team member takes one. Only example.com emails: this is for made-up users, never a
real person's. The password comes only from NEW_PASSWORD, as for set_password.py, and is never
printed or logged. An email a user has already changes nothing, and it says so.
"""

import argparse
import os
import sys

from countertalk import auth, users
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.logs import configure_logging
from countertalk.models import Role

VARIABLE = "NEW_PASSWORD"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="add_user.py")
    parser.add_argument("org_id")
    parser.add_argument("email", help="an example.com email")
    parser.add_argument("name")
    parser.add_argument("role", choices=list(Role))
    parser.add_argument("--team", help="a team member's team, by its key")
    args = parser.parse_args(argv)
    password = os.environ.get(VARIABLE, "")
    if not password:
        print(f"no password: set {VARIABLE} for this command", file=sys.stderr)
        return 1
    try:
        with session_scope() as session:
            found = users.add_user(
                session, args.org_id, args.email, args.name, args.role, args.team, password
            )
            email = None if found is None else found.email
    except SETUP_ERRORS as error:
        print(f"user not added: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:
        print(f"user not added: {describe(error)}", file=sys.stderr)
        return 1
    if email is None:
        print(f"a user has that email already: nothing changed ({auth.normal_email(args.email)})")
        return 0
    team = f" in team {args.team}" if args.team else ""
    print(f"added {email}, {args.role}{team}: sign in with the password given")
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
