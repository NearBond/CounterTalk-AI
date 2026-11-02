"""Sets a user's password, so they can sign in (batch 16): a dev and admin tool.

    NEW_PASSWORD=<password> uv run python scripts/set_password.py <email>

The password comes only from the NEW_PASSWORD environment variable: never an argument, which
would sit in the shell's history and the process list. It's never printed or logged. It needs
PASSWORD_MIN_CHARS characters or more. Setting it ends the user's sessions, so anyone signed in
with the old one signs in again.
"""

import argparse
import os
import sys

from countertalk import auth
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.logs import configure_logging

VARIABLE = "NEW_PASSWORD"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="set_password.py")
    parser.add_argument("email", help=f"the user's email; the password comes from {VARIABLE}")
    args = parser.parse_args(argv)
    password = os.environ.get(VARIABLE, "")
    if not password:
        print(f"no password: set {VARIABLE} for this command", file=sys.stderr)
        return 1
    try:
        with session_scope() as session:
            found = auth.set_password(session, args.email, password)
    except SETUP_ERRORS as error:
        print(f"password not set: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:  # too short
        print(f"password not set: {describe(error)}", file=sys.stderr)
        return 1
    if not found:
        print(f"no user has the email {args.email!r}", file=sys.stderr)
        return 1
    print(f"password set for {auth.normal_email(args.email)}; their sessions are ended")
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
