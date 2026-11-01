"""Checks this machine is ready for CounterTalk: `uv run python scripts/check_setup.py`.

Each line says ok, skip (a key that isn't needed yet) or fail (fix it first). Exits with 1 if
any line failed. Key values and the database password are never printed.

`--database` checks only the settings and the database, with hints that suit any host. start.sh
runs it before the migrations, so a wrong DATABASE_URL stops the service with a plain reason.
"""

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from countertalk.config import get_settings
from countertalk.db import get_engine
from countertalk.limits import SETUP_CHECK_TIMEOUT_MS

# pyproject.toml's requires-python; a test keeps the two the same.
REQUIRED_PYTHON = (3, 12)
KEYS = ("MODEL_API_KEY", "SECRET_KEY")
# Server messages that point at DATABASE_URL, not at a server that's down. A refused connection
# has no SQLSTATE in psycopg, only the message. The message itself is never printed.
REFUSED = [
    (re.compile(r"password authentication failed"), "the password was refused"),
    (re.compile(r'role "[^"]*" does not exist'), "the user doesn't exist"),
    (re.compile(r'database "[^"]*" does not exist'), "the database doesn't exist"),
]
# Where the database runs differs: on this laptop it's compose's, on a host it's the host's.
CANT_CONNECT_HERE = "can't connect; is Postgres running? (docker compose up -d db)"
CANT_CONNECT_ANYWHERE = "can't connect; is the database running, and is DATABASE_URL's host right?"


@dataclass(frozen=True)
class Check:
    status: Literal["ok", "skip", "fail"]
    name: str
    detail: str


def check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] == REQUIRED_PYTHON:
        return Check("ok", "python", version)
    needed = ".".join(str(part) for part in REQUIRED_PYTHON)
    return Check("fail", "python", f"{version}; CounterTalk needs {needed}")


def check_tool(name: str, version_args: list[str]) -> Check:
    path = shutil.which(name)
    if path is None:
        return Check("fail", name, "not found on PATH")
    try:
        result = subprocess.run(
            [path, *version_args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=SETUP_CHECK_TIMEOUT_MS / 1000,
            check=True,
        )
    except subprocess.TimeoutExpired:
        return Check("fail", name, f"timed out after {SETUP_CHECK_TIMEOUT_MS} ms")
    except subprocess.CalledProcessError as error:
        return Check("fail", name, f"exit code {error.returncode}")
    except OSError as error:
        return Check("fail", name, f"can't run it: {type(error).__name__}")
    lines = (result.stdout or result.stderr).strip().splitlines()
    return Check("ok", name, lines[0].split(" Copyright")[0] if lines else "runs")


def check_settings() -> Check:
    try:
        get_settings()
    except ValidationError as error:
        fields = ", ".join(sorted({str(problem["loc"][0]) for problem in error.errors()}))
        return Check("fail", "settings", f"invalid in backend/.env or the environment: {fields}")
    return Check("ok", "settings", "valid")


def check_database(cant_connect: str = CANT_CONNECT_HERE) -> Check:
    url = make_url(get_settings().DATABASE_URL)
    where = f"{url.host}:{url.port or 5432}/{url.database}"
    try:
        with get_engine().connect() as conn:
            conn.execute(select(1))
    except (DBAPIError, PoolTimeoutError) as error:
        return Check("fail", "postgres", f"{where}: {_why_refused(str(error), cant_connect)}")
    return Check("ok", "postgres", where)


def _why_refused(message: str, cant_connect: str) -> str:
    return next((why for pattern, why in REFUSED if pattern.search(message)), cant_connect)


def check_keys() -> list[Check]:
    settings = get_settings()
    return [
        Check("ok", name, "set")
        if getattr(settings, name).get_secret_value()
        else Check("skip", name, "not set; add it to backend/.env when it's needed")
        for name in KEYS
    ]


def report(checks: list[Check]) -> int:
    for check in checks:
        print(f"{check.status:<5} {check.name}: {check.detail}")
    return 1 if any(check.status == "fail" for check in checks) else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Checks this machine is ready for CounterTalk.")
    parser.add_argument(
        "--database", action="store_true", help="check only the settings and the database"
    )
    if parser.parse_args(argv).database:
        checks = [check_settings()]
        if checks[0].status == "ok":
            checks.append(check_database(CANT_CONNECT_ANYWHERE))
        return report(checks)
    checks = [
        check_python(),
        check_tool("uv", ["--version"]),
        check_tool("ffmpeg", ["-version"]),
        check_tool("ffprobe", ["-version"]),
        check_settings(),
    ]
    if checks[-1].status == "ok":
        checks += [check_database(), *check_keys()]
    return report(checks)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
