"""Shared test setup (CODING_GUIDELINES §3.3).

- Every setting is pinned before the app reads one, so a developer's .env or shell can't change
  results. DATA_DIR is each test's own tmp_path.
- countertalk_test, on the Postgres from compose.yaml, is created if it's missing and migrated
  once per session. Every table is emptied before each test.
"""

import os
import shutil
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from countertalk import auth, transcription
from countertalk.config import BACKEND_DIR, get_settings
from countertalk.db import get_engine, session_scope
from countertalk.main import create_app
from countertalk.models import Org, Team, User, Visit

# 127.0.0.1, not localhost: on Windows, localhost tries ::1 first and waits for it to time out.
# CI's job container reaches its Postgres service by name instead (TEST_POSTGRES_HOST).
POSTGRES_HOST = os.environ.get("TEST_POSTGRES_HOST", "127.0.0.1")
SERVER_URL = f"postgresql+psycopg://countertalk:countertalk@{POSTGRES_HOST}:5432"
TEST_DATABASE = "countertalk_test"

# Every setting except DATA_DIR (each test's tmp_path) and WEB_DIST_DIR (pinned per session).
PINNED_SETTINGS = {
    "DATABASE_URL": f"{SERVER_URL}/{TEST_DATABASE}",
    "DEBUG": "0",
    "MODEL_API_KEY": "",
    "SECRET_KEY": "test-only-secret",
}


def alembic_config() -> Config:
    return Config(toml_file=str(BACKEND_DIR / "pyproject.toml"))


def create_database(name: str) -> None:
    """Creates the database on the test server if it's missing."""
    engine = create_engine(f"{SERVER_URL}/postgres", isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if exists is None:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()


def drop_database(name: str) -> None:
    engine = create_engine(f"{SERVER_URL}/postgres", isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        engine.dispose()


def terminate_backends(pid: int | None = None) -> None:
    """Kills server connections to the test database, as a restart or a timeout would.

    Only connection `pid` if given; otherwise every connection but the caller's own.
    """
    engine = create_engine(f"{SERVER_URL}/postgres", isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = :name AND pid <> pg_backend_pid()"
                    " AND (CAST(:pid AS integer) IS NULL OR pid = :pid)"
                ),
                {"name": TEST_DATABASE, "pid": pid},
            )
    finally:
        engine.dispose()


def open_transactions() -> int:
    """Transactions open on the test database, from any client connection but this one.
    Clients only: autovacuum's workers show here too, with transactions of their own."""
    engine = create_engine(f"{SERVER_URL}/postgres", isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            return conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = :name"
                    " AND backend_type = 'client backend'"
                    " AND xact_start IS NOT NULL AND pid <> pg_backend_pid()"
                ),
                {"name": TEST_DATABASE},
            )
    finally:
        engine.dispose()


def lock_waiters() -> int:
    """Connections to the test database waiting for a lock: a race test waits for its other
    side to block before letting it go on."""
    engine = create_engine(f"{SERVER_URL}/postgres", isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            return conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = :name"
                    " AND wait_event_type = 'Lock'"
                ),
                {"name": TEST_DATABASE},
            )
    finally:
        engine.dispose()


TWO_TURNS = (("c0:A", 0, "Namaste ji."), ("c0:B", 1, "Haan bolo."))


def give_transcript(visit_id: str, lines: Sequence[tuple[str, int, str]] = TWO_TURNS) -> str:
    """Gives the visit a transcript of these turns (label, start_ms, text), as the transcribe
    job would, each turn 1 ms long. Its ID."""
    turns = [
        {
            "turn_id": index,
            "speaker_label": label,
            "start_ms": start_ms,
            "end_ms": start_ms + 1,
            "text": text,
        }
        for index, (label, start_ms, text) in enumerate(lines)
    ]
    with session_scope() as session:
        visit = session.get_one(Visit, visit_id)
        return transcription.add_transcript(session, visit, "fake", "fake-1", "hi,en", turns)


def org_count() -> int:
    with session_scope(read_only=True) as session:
        return session.scalar(select(func.count()).select_from(Org)) or 0


@pytest.fixture(scope="session", autouse=True)
def _pinned_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    with pytest.MonkeyPatch.context() as mp:
        for name, value in PINNED_SETTINGS.items():
            mp.setenv(name, value)
        mp.setenv("DATA_DIR", str(tmp_path_factory.mktemp("session-data")))
        # No built web app unless a test makes one (tests/test_web_app.py).
        mp.setenv("WEB_DIST_DIR", str(tmp_path_factory.mktemp("no-web-app") / "dist"))
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def _test_database(_pinned_environment: None) -> None:
    try:
        create_database(TEST_DATABASE)
    except OperationalError:
        pytest.exit(
            f"Postgres isn't reachable on {POSTGRES_HOST}:5432: run `docker compose up -d db`"
        )
    command.upgrade(alembic_config(), "head")


@pytest.fixture(autouse=True)
def _fresh_test(
    _test_database: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    wipe_database()
    yield
    get_settings.cache_clear()


def wipe_database() -> None:
    """Every table emptied, as each test starts: a test that needs a fresh start mid-way too."""
    with get_engine().begin() as conn:
        tables = conn.scalars(
            text(
                "SELECT quote_ident(tablename) FROM pg_tables"
                " WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ).all()
        if tables:
            conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


@pytest.fixture
def app() -> FastAPI:
    """A fresh app. Tests may add their own routes to it before making requests."""
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # https: the session cookie is Secure, so a client sends it only over https (batch 16).
    with TestClient(app, base_url=HTTPS, raise_server_exceptions=False) as test_client:
        yield test_client


HTTPS = "https://testserver"


@contextmanager
def signed_in_as(client: TestClient, user_id: str) -> Iterator[None]:
    """The client as this user for a while (a rep confirming, say), then as it was."""
    before = client.cookies.get(auth.COOKIE)
    sign_in(client, user_id)
    try:
        yield
    finally:
        client.cookies.delete(auth.COOKIE)
        if before is not None:
            client.cookies.set(auth.COOKIE, before)


def a_user(org_id: str, role: str, name: str, team: str | None = None) -> str:
    """A user of the org with this role (a team member of the team with that key): their ID."""
    with session_scope() as session:
        team_id = None
        if team is not None:
            team_id = session.scalars(
                select(Team.id).where(Team.org_id == org_id, Team.key == team)
            ).one()
        email = f"{name.lower().replace(' ', '-')}-{org_id[:8]}@example.com"
        user = User(org_id=org_id, email=email, name=name, role=role, team_id=team_id)
        session.add(user)
        session.flush()
        return user.id


def sign_in(client: TestClient, user_id: str) -> None:
    """Signs the client in as this user: a real session and its signed cookie, as sign-in
    sets them (batch 16). No password: argon2 is slow, and only test_auth needs one."""
    with session_scope() as session:
        token = auth.start_session(session, session.get_one(User, user_id))
    client.cookies.set(auth.COOKIE, auth.cookie_for(token))


ORG_PACK_FIXTURES = BACKEND_DIR / "tests" / "fixtures" / "org_packs"


@pytest.fixture
def pack_roots(tmp_path: Path) -> tuple[Path, Path]:
    """Copies of the tests' org packs and the real playbooks, to change or break (batch 8)."""
    from countertalk.playbooks import PLAYBOOKS_DIR

    packs, books = tmp_path / "org_packs", tmp_path / "playbooks"
    shutil.copytree(ORG_PACK_FIXTURES, packs)
    shutil.copytree(PLAYBOOKS_DIR, books)
    return packs, books


def edit_file(path: Path, old: str, new: str) -> None:
    """Replaces one exact piece of a text file, which must be there."""
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} isn't in {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


@pytest.fixture
def live_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """The real MODEL_API_KEY from backend/.env, for a live test (§3.2); skips without one."""
    from countertalk.config import Settings

    # The tests pin MODEL_API_KEY empty; without that, the settings read backend/.env.
    monkeypatch.delenv("MODEL_API_KEY")
    key = Settings().MODEL_API_KEY.get_secret_value()
    if not key:
        pytest.skip("MODEL_API_KEY isn't set in backend/.env")
    return key


def print_utf8(line: str) -> None:
    """For a live test's reader, who runs it with -s. As UTF-8 bytes: on Windows, output that's
    piped (as a tool runs it) is cp1252, and print() can't write Devanagari there."""
    sys.stdout.flush()
    sys.stdout.buffer.write(line.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
