"""Shared test setup (CODING_GUIDELINES §3.3).

- Every setting is pinned before the app reads one, so a developer's .env or shell can't change
  results. DATA_DIR is each test's own tmp_path.
- countertalk_test, on the Postgres from compose.yaml, is created if it's missing and migrated
  once per session. Every table is emptied before each test.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError

from countertalk.config import BACKEND_DIR, get_settings
from countertalk.db import get_engine, session_scope
from countertalk.main import create_app
from countertalk.models import Org

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
    with get_engine().begin() as conn:
        tables = conn.scalars(
            text(
                "SELECT quote_ident(tablename) FROM pg_tables"
                " WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ).all()
        if tables:
            conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    yield
    get_settings.cache_clear()


@pytest.fixture
def app() -> FastAPI:
    """A fresh app. Tests may add their own routes to it before making requests."""
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
