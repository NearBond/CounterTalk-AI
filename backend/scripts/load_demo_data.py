"""Loads the PepsiCo India demo org, with its recordings, into a fresh database: a clone of the
repo then shows the demo's data (batch 20).

    uv run alembic upgrade head
    uv run python scripts/load_demo_data.py

Run from backend/. It reads DATABASE_URL and DATA_DIR as the app does (backend/.env), and refuses
unless the database is migrated to the code's head and holds no org yet.
- The data is demo/pepsico-india.sql at the repo's root, a data-only pg_dump. It goes in on one
  connection of its own, in one transaction, with no psql needed. Each table's triggers are off
  while its rows go in, foreign keys included (visits and transcripts point at each other), so the
  database user must be a superuser, as docker compose's is. They're on again before the commit.
- Then each visit's playback.m4a goes from demo/visits/<id>/ to DATA_DIR/visits/<id>/: all that
  the play button needs. Nothing is processed, so no Meta key is needed.
- It prints the counts and the accounts to sign in as, all with the same password.
"""

import argparse
import shutil
import sys
from collections.abc import Iterator

import psycopg
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from countertalk import demo_accounts, storage
from countertalk.config import BACKEND_DIR, get_settings
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.logs import configure_logging
from countertalk.models import Issue, Org, Shop, User, Visit

DEMO_DIR = BACKEND_DIR.parent / "demo"
SQL = DEMO_DIR / "pepsico-india.sql"
RECORDINGS = DEMO_DIR / "visits"
# Every demo account's password: its hashes are in the SQL.
PASSWORD = "metahackathon"


class Refused(Exception):
    """A reason not to load, said in one line."""


def statements(sql: str) -> Iterator[tuple[str, str | None]]:
    """The dump's statements in order: (statement, None), or (a COPY, its rows). Comments and
    psql's own backslash commands are skipped."""
    lines = iter(sql.split("\n"))
    pending: list[str] = []
    for line in lines:
        if not pending and (not line.strip() or line.startswith(("--", "\\"))):
            continue
        if not pending and line.startswith("COPY ") and line.endswith(" FROM stdin;"):
            rows = []
            for row in lines:
                if row == "\\.":
                    break
                rows.append(row + "\n")
            else:
                raise Refused(f"{SQL.name}: a COPY has no end")
            yield line.removesuffix(";"), "".join(rows)
            continue
        pending.append(line)
        if line.rstrip().endswith(";"):
            yield "\n".join(pending), None
            pending = []
    if pending:
        raise Refused(f"{SQL.name} ends in the middle of a statement")


def check_empty_and_migrated() -> None:
    head = ScriptDirectory.from_config(Config(toml_file=str(BACKEND_DIR / "pyproject.toml")))
    with session_scope(read_only=True) as session:
        migrated = session.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
        version = (
            session.scalar(text("SELECT version_num FROM alembic_version")) if migrated else None
        )
        if version != head.get_current_head():
            raise Refused("the database isn't migrated to head: run `uv run alembic upgrade head`")
        orgs = session.scalar(select(func.count()).select_from(Org)) or 0
    if orgs:
        raise Refused(f"the database already has {orgs} org(s): this loads into an empty one only")


def load(sql: str) -> None:
    """The SQL in one transaction, on a connection of its own: its SETs (search_path among them)
    never reach the app's pooled connections."""
    url = make_url(get_settings().DATABASE_URL).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False), connect_timeout=10) as conn:
        with conn.cursor() as cursor:
            for statement, rows in statements(sql):
                if rows is None:
                    cursor.execute(statement)
                else:
                    with cursor.copy(statement) as copy:
                        copy.write(rows)
        # Leaving the block commits; an error on the way rolls all of it back.


def copy_recordings() -> int:
    with session_scope(read_only=True) as session:
        visits = {str(v) for v in session.scalars(select(Visit.id))}
    copied = 0
    for folder in sorted(RECORDINGS.iterdir()):
        source = folder / "playback.m4a"
        if folder.name not in visits or not source.is_file():
            continue
        target = storage.playback_path(folder.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied += 1
    return copied


def report(copied: int) -> None:
    with session_scope(read_only=True) as session:
        orgs = session.scalars(select(Org.name)).all()
        statuses = dict(
            session.execute(select(Visit.status, func.count()).group_by(Visit.status)).all()
        )
        shops = session.scalar(select(func.count()).select_from(Shop))
        issues = session.scalar(select(func.count()).select_from(Issue))
        emails = set(session.scalars(select(User.email).where(User.password_hash.is_not(None))))
    visits = sum(statuses.values())
    by_status = ", ".join(f"{count} {status}" for status, count in sorted(statuses.items()))
    print(
        f"loaded {', '.join(orgs)}: {visits} visits ({by_status}), {shops} shops, {issues} issues"
    )
    print(f"recordings: {copied} playback files in {storage.data_dir() / 'visits'}")
    print(f"sign in with any of these, password {PASSWORD}:")
    for account in demo_accounts.ACCOUNTS:
        if account.email in emails:
            note = "" if account.note is None else f" - {account.note}"  # ASCII: any console
            print(f"  {account.email}  {account.label}{note}")
    print(
        f"to list them on the sign-in page too, set DEMO_SIGNIN_PASSWORD={PASSWORD} in backend/.env"
    )


def main(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="load_demo_data.py", description=__doc__).parse_args(argv)
    try:
        if not SQL.is_file() or not RECORDINGS.is_dir():
            raise Refused(f"{SQL} or {RECORDINGS} is missing: run this from a clone of the repo")
        check_empty_and_migrated()
        load(SQL.read_text(encoding="utf-8"))
        copied = copy_recordings()
        report(copied)
    except Refused as error:
        print(f"not loaded: {error}", file=sys.stderr)
        return 1
    except SETUP_ERRORS as error:
        print(f"not loaded: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except psycopg.errors.InsufficientPrivilege:
        print(
            "not loaded: the database user must be a superuser (it turns triggers off)",
            file=sys.stderr,
        )
        return 1
    except psycopg.Error as error:
        print(f"not loaded: {type(error).__name__}, rolled back", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
