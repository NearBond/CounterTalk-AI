"""scripts/start.sh: migrate first, then serve; stop everything if any part stops (batch 2)."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text

from countertalk.config import BACKEND_DIR
from tests.conftest import PINNED_SETTINGS, SERVER_URL, create_database, drop_database

BASH = shutil.which("bash")
START = BACKEND_DIR / "scripts" / "start.sh"
SCRATCH_DATABASE = "countertalk_test_start"
RUN_TIMEOUT_S = 60

pytestmark = pytest.mark.skipif(BASH is None, reason="start.sh needs bash")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_env(database_url: str, port: int) -> dict[str, str]:
    # The venv's alembic and uvicorn come first, as they do in the image.
    venv_bin = str(Path(sys.executable).parent)
    return {
        **os.environ,
        "PATH": venv_bin + os.pathsep + os.environ["PATH"],
        "DATABASE_URL": database_url,
        "PORT": str(port),
    }


def run_start(database_url: str, port: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(START)],
        env=start_env(database_url, port),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=RUN_TIMEOUT_S,
    )


@pytest.fixture
def broken_database() -> Iterator[str]:
    """A database the first migration can't apply to: `orgs` is already there."""
    drop_database(SCRATCH_DATABASE)
    create_database(SCRATCH_DATABASE)
    url = f"{SERVER_URL}/{SCRATCH_DATABASE}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE orgs (id integer)"))
    engine.dispose()
    yield url
    drop_database(SCRATCH_DATABASE)


def test_a_failed_migration_stops_start_sh_before_the_server_starts(broken_database: str) -> None:
    result = run_start(broken_database, free_port())

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "DuplicateTable" in output
    # The error's type and frames are printed, never its message: a data migration's error
    # can quote row values (§1.9).
    assert "already exists" not in output
    assert "Uvicorn running" not in output
    assert "Started server process" not in output
    engine = create_engine(broken_database)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    # The failed migration rolled back whole: nothing half-made is left behind.
    assert tables == {"orgs"}


def test_start_sh_exits_with_an_error_when_the_server_dies() -> None:
    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        port = taken.getsockname()[1]

        result = run_start(PINNED_SETTINGS["DATABASE_URL"], port)

    output = result.stdout + result.stderr
    assert result.returncode != 0
    # It got past the migrations, and it was the server that stopped.
    assert "Context impl PostgresqlImpl" in output
    assert "error while attempting to bind" in output


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can't send SIGTERM to bash")
def test_sigterm_reaches_the_server_and_start_sh_exits() -> None:
    port = free_port()
    process = subprocess.Popen(
        [BASH, str(START)], env=start_env(PINNED_SETTINGS["DATABASE_URL"], port), text=True
    )
    try:
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while True:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert process.poll() is None, "start.sh exited before the server answered"
            assert time.monotonic() < deadline, "the server didn't answer"
            time.sleep(0.2)

        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=RUN_TIMEOUT_S) != 0
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=1)
    finally:
        if process.poll() is None:
            process.kill()


SLOW_ALEMBIC = """#!/usr/bin/env bash
trap 'echo "got TERM" > "$ALEMBIC_MARKER"; exit 143' TERM
echo "started" > "$ALEMBIC_MARKER"
sleep 30 &
wait "$!"
"""


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can't send SIGTERM to bash")
def test_sigterm_during_the_migrations_stops_them_and_start_sh_exits(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    alembic = fake_bin / "alembic"
    alembic.write_text(SLOW_ALEMBIC, encoding="utf-8")
    alembic.chmod(0o755)
    marker = tmp_path / "alembic.marker"
    env = start_env(PINNED_SETTINGS["DATABASE_URL"], free_port())
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env["ALEMBIC_MARKER"] = str(marker)
    process = subprocess.Popen([BASH, str(START)], env=env)
    try:
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while not marker.exists():
            assert time.monotonic() < deadline, "the migrations didn't start"
            time.sleep(0.05)

        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=10) == 143
        assert marker.read_text(encoding="utf-8").strip() == "got TERM"
    finally:
        if process.poll() is None:
            process.kill()


def test_a_wrong_database_password_stops_start_sh_before_the_migrations() -> None:
    url = f"{SERVER_URL.replace(':countertalk@', ':not-the-pw@')}/{SCRATCH_DATABASE}"

    result = run_start(url, free_port())

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "the password was refused" in output
    assert "not-the-pw" not in output
    # Neither Alembic nor the server started.
    assert "Context impl" not in output
    assert "Uvicorn running" not in output


SLOW_PYTHON = SLOW_ALEMBIC.replace("ALEMBIC_MARKER", "PYTHON_MARKER")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can't send SIGTERM to bash")
def test_sigterm_during_the_database_check_stops_it_and_start_sh_exits(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    python = fake_bin / "python"
    python.write_text(SLOW_PYTHON, encoding="utf-8")
    python.chmod(0o755)
    marker = tmp_path / "python.marker"
    env = start_env(PINNED_SETTINGS["DATABASE_URL"], free_port())
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
    env["PYTHON_MARKER"] = str(marker)
    process = subprocess.Popen([BASH, str(START)], env=env)
    try:
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while not marker.exists():
            assert time.monotonic() < deadline, "the database check didn't start"
            time.sleep(0.05)

        process.send_signal(signal.SIGTERM)

        assert process.wait(timeout=10) == 143
        assert marker.read_text(encoding="utf-8").strip() == "got TERM"
    finally:
        if process.poll() is None:
            process.kill()
