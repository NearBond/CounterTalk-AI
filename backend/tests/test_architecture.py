"""Rules from CODING_GUIDELINES §1.7, §2.1, §2.3, §2.4 and §2.7, checked on the app's source.

Only the countertalk package is scanned: scripts may print and run tools, and tests may use httpx.
Rules about modules that don't exist yet (audio.py, worker.py, stt/, llm/) pass until they do.
Every rule also runs on small examples, so a rule that stops finding anything fails too.
"""

import ast
import re
import sys
from collections.abc import Callable, Iterator

import pytest

from countertalk.config import BACKEND_DIR

APP_DIR = BACKEND_DIR / "countertalk"

# (path inside countertalk/, parsed source)
Source = tuple[str, ast.Module]


def app_sources() -> list[Source]:
    return [
        (path.relative_to(APP_DIR).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(APP_DIR.rglob("*.py"))
    ]


def dotted(node: ast.AST) -> str:
    """'os.environ' for os.environ; '' unless it's a chain of plain names."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def imported_as(tree: ast.Module) -> dict[str, str]:
    """What each imported name stands for: {'o': 'os'} for `import os as o`."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.asname: alias.name for alias in node.names if alias.asname}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found |= {
                alias.asname or alias.name: f"{node.module}.{alias.name}" for alias in node.names
            }
    return found


def names(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Every import, and every dotted name used, with import aliases resolved.

    'os.environ' for `from os import environ`, and for `import os as o` then `o.environ`.
    """
    aliases = imported_as(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.lineno, node.module
            yield from ((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Name | ast.Attribute) and (name := dotted(node)):
            root, dot, rest = name.partition(".")
            yield node.lineno, aliases.get(root, root) + dot + rest


def matches(name: str, banned: set[str]) -> bool:
    return any(name == item or name.startswith(f"{item}.") for item in banned)


def method_calls(tree: ast.Module, methods: set[str]) -> Iterator[ast.Call]:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in methods
        ):
            yield node


def banned_names(
    sources: list[Source], banned: set[str], allowed: Callable[[str], bool]
) -> list[str]:
    return [
        f"{path}:{line} {name}"
        for path, tree in sources
        if not allowed(path)
        for line, name in names(tree)
        if matches(name, banned)
    ]


def only_db_commits(sources: list[Source]) -> list[str]:
    """§2.3: domain functions never commit; db.py's session_scope does."""
    return [
        f"{path}:{call.lineno}"
        for path, tree in sources
        if path != "db.py"
        for call in method_calls(tree, {"commit"})
    ]


def settings_only_through_get_settings(sources: list[Source]) -> list[str]:
    """§1.7: no os.environ, os.getenv or .env reading outside config.py."""
    banned = {"os.environ", "os.getenv", "os.putenv", "dotenv", "pydantic_settings"}
    found = banned_names(sources, banned, lambda path: path == "config.py")
    return found + [
        f"{path}:{node.lineno} Settings()"
        for path, tree in sources
        if path != "config.py"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and dotted(node.func).split(".")[-1] == "Settings"
    ]


# The SDKs that reach outside services: only stt/ and llm/ may import them.
EXTERNAL_SDKS = {"openai", "httpx", "websockets"}


def external_sdks_only_in_clients(sources: list[Source]) -> list[str]:
    """§2.4: Meta is called only from the client modules in stt/ and llm/."""
    return banned_names(sources, EXTERNAL_SDKS, lambda path: path.startswith(("stt/", "llm/")))


def subprocesses_only_in_audio(sources: list[Source]) -> list[str]:
    """§2.7: ffmpeg and ffprobe run only from audio.py."""
    banned = {"subprocess", "asyncio.subprocess", "asyncio.create_subprocess_exec"}
    banned |= {"asyncio.create_subprocess_shell", "os.system", "os.popen"}
    found = banned_names(sources, banned, lambda path: path == "audio.py")
    return found + [
        f"{path}:{line} {name}"
        for path, tree in sources
        if path != "audio.py"
        for line, name in names(tree)
        if name.startswith(("os.spawn", "os.exec", "os.posix_spawn"))
    ]


def routers_stay_thin(sources: list[Source]) -> list[str]:
    """§2.1: no SQL in api/. A router may name the Session type, and calls domain functions."""
    session_methods = {"execute", "scalar", "scalars", "add", "add_all", "delete", "flush"}
    session_methods |= {"get", "merge", "query", "refresh", "commit", "rollback"}
    found = []
    for path, tree in sources:
        if not path.startswith("api/"):
            continue
        found += [
            f"{path}:{line} {name}"
            for line, name in names(tree)
            if matches(name, {"sqlalchemy"})
            and name not in {"sqlalchemy.orm", "sqlalchemy.orm.Session"}
        ]
        sessions = session_names(tree)
        found += [
            f"{path}:{call.lineno} {dotted(call.func)}"
            for call in method_calls(tree, session_methods)
            if dotted(call.func).split(".")[0] in sessions
        ]
    return found


def session_names(tree: ast.Module) -> set[str]:
    """`session`, and every parameter typed as a session, whatever it's called."""
    return {"session"} | {
        node.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.arg)
        and node.annotation is not None
        and dotted(node.annotation).split(".")[-1] in {"DbSession", "Session"}
    }


def no_sleeps_or_retry_loops_outside_worker(sources: list[Source]) -> list[str]:
    """§2.4: retries live in the job queue (and the client modules' SDKs), never ad hoc."""
    banned = {"time.sleep", "asyncio.sleep", "anyio.sleep", "tenacity", "backoff", "retrying"}
    found = banned_names(sources, banned, lambda path: path == "worker.py")
    return found + [
        f"{path}:{node.lineno} while True"
        for path, tree in sources
        if path != "worker.py"
        for node in ast.walk(tree)
        if isinstance(node, ast.While)
        and isinstance(node.test, ast.Constant)
        and node.test.value is True
    ]


SESSION_LEVEL_SET = re.compile(r"\s*SET\s+(?!LOCAL\b|TRANSACTION\b)", re.IGNORECASE)


def sql_strings(tree: ast.Module) -> Iterator[ast.Constant]:
    """The SQL text passed to text(), execute() and exec_driver_sql(), as written."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else dotted(func)
        sql = node.args[0]
        if isinstance(sql, ast.JoinedStr) and sql.values:
            sql = sql.values[0]
        if name in {"text", "execute", "exec_driver_sql"} and isinstance(sql, ast.Constant):
            if isinstance(sql.value, str):
                yield sql


def no_session_level_set(sources: list[Source]) -> list[str]:
    """A session-level SET stays on the pooled connection for whoever uses it next.

    Use SET LOCAL, or SET TRANSACTION. Found by review in batch 1.
    """
    return [
        f"{path}:{sql.lineno}"
        for path, tree in sources
        for sql in sql_strings(tree)
        if SESSION_LEVEL_SET.match(sql.value)
    ]


def visit_status_only_through_set_status(sources: list[Source]) -> list[str]:
    """§2.5: a visit's status changes only in visits.set_status, which checks the transition."""
    found = []
    for path, tree in sources:
        allowed = {
            id(node)
            for function in ast.walk(tree)
            if path == "visits.py"
            and isinstance(function, ast.FunctionDef)
            and function.name == "set_status"
            for node in ast.walk(function)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            found += [
                f"{path}:{node.lineno} {name}"
                for target in targets
                if (name := dotted(target)) == "visit.status" or name.endswith(".visit.status")
                if id(node) not in allowed
            ]
    return found


def get_session_only_through_db_session(sources: list[Source]) -> list[str]:
    """Routes take DbSession: Depends(get_session) without scope="function" commits after the
    response is sent, so a failed commit would reach the client as a success."""
    return [
        f"{path}:{line} {name}"
        for path, tree in sources
        if path != "db.py"
        for line, name in names(tree)
        if name.split(".")[-1] == "get_session"
    ]


def routers_carry_their_own_prefix(sources: list[Source]) -> list[str]:
    """Each router in api/ is APIRouter(prefix="/api/<area>"), and main.py adds no prefix.

    A route's template, which the 500 log line names, holds only the router's own prefix.
    Found by review in batch 3.
    """
    found = []
    for path, tree in sources:
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = dotted(call.func).split(".")[-1]
            keywords = {k.arg: k.value for k in call.keywords}
            if name == "include_router" and "prefix" in keywords:
                found.append(f"{path}:{call.lineno} include_router(prefix=...)")
            if name == "APIRouter" and path.startswith("api/"):
                prefix = keywords.get("prefix")
                if not (
                    isinstance(prefix, ast.Constant)
                    and isinstance(prefix.value, str)
                    and prefix.value.startswith("/api/")
                ):
                    found.append(f"{path}:{call.lineno} APIRouter without a /api/ prefix")
    return found


CLIENT_MODULES = {
    "countertalk.config",
    "countertalk.errors",
    "countertalk.limits",
    "countertalk.logs",
}


def imported_modules(path: str, tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Each module an import names, in full: `from countertalk import errors` and, in jobs.py,
    `from . import errors` both name countertalk.errors."""
    package = ["countertalk", *path.split("/")[:-1]]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative: up from this file's package
                parent = package[: len(package) - node.level + 1]
                base = ".".join(parent + ([base] if base else []))
            if base in ("countertalk", ".".join(package)) or not node.module:
                yield from ((node.lineno, f"{base}.{alias.name}") for alias in node.names)
            else:
                yield node.lineno, base


def clients_import_only_the_allowlist(sources: list[Source]) -> list[str]:
    """§2.1: stt/ and llm/ import only the standard library, their SDK, and config, errors,
    limits and logs. An allowlist, so a module added later is covered too."""
    found = []
    for path, tree in sources:
        if not path.startswith(("stt/", "llm/")):
            continue
        for line, module in imported_modules(path, tree):
            top = module.split(".")[0]
            if top in sys.stdlib_module_names or top in EXTERNAL_SDKS:
                continue
            if module in CLIENT_MODULES or matches(module, {"countertalk.stt", "countertalk.llm"}):
                continue
            found.append(f"{path}:{line} {module}")
    return found


JOBS_MAY_IMPORT = {"countertalk", "countertalk.db", "countertalk.errors", "countertalk.limits"}
JOBS_MAY_IMPORT |= {"countertalk.models"}


def jobs_imports_no_domain_module(sources: list[Source]) -> list[str]:
    """jobs.py imports no domain module: from batch 6, visits.complete queues a job, so that
    would be a cycle. The worker passes fail_processing in instead."""
    return [
        f"{path}:{line} {module}"
        for path, tree in sources
        if path == "jobs.py"
        for line, module in imported_modules(path, tree)
        if matches(module, {"countertalk"}) and module not in JOBS_MAY_IMPORT
    ]


def job_times_come_from_postgres(sources: list[Source]) -> list[str]:
    """jobs.py compares and sets job times with Postgres's now(), never Python's clock.

    Workers on different machines must agree on what is due and what is stale (batch 4).
    """
    banned = {
        "countertalk.models.utc_now",
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "datetime.datetime.today",
        "datetime.date.today",
        "time.time",
        "time.time_ns",
    }
    return banned_names(sources, banned, lambda path: path != "jobs.py")


MODE = re.compile(r"[rwxabt+]+")
# Writing or moving a file by name, outside storage.py's temporary-file-then-replace.
WRITERS = {"os.replace", "os.rename", "os.renames", "os.open", "os.fdopen", "shutil.move"}
WRITERS |= {"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree"}


def _writes(call: ast.Call) -> bool:
    """Whether an open() call was given a writing mode: the second argument (open, wave.open,
    gzip.open …), the first (Path.open), or mode=. Only a string that is all mode letters counts,
    so a file name ("talk.wav") is never taken for one."""
    given = [arg for arg in call.args[:2]] + [k.value for k in call.keywords if k.arg == "mode"]
    return any(
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and MODE.fullmatch(arg.value) is not None
        and any(letter in arg.value for letter in "wax+")
        for arg in given
    )


def _writing_files(tree: ast.Module) -> set[str]:
    """Names bound by `with storage.writing(...) as name`: files storage.py moves into place."""
    return {
        item.optional_vars.id
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        for item in node.items
        if isinstance(item.context_expr, ast.Call)
        and dotted(item.context_expr.func).endswith("writing")
        and isinstance(item.optional_vars, ast.Name)
    }


def files_written_only_by_storage(sources: list[Source]) -> list[str]:
    """R3: files are written only by storage.py: a temporary file, fsync, os.replace, then the
    folder fsynced. wave may write only into a file storage.writing() gave it. (`.replace` is
    left alone: str.replace looks the same.)"""
    found = []
    for path, tree in sources:
        if path == "storage.py":
            continue
        found += [f"{path}:{line} {name}" for line, name in names(tree) if matches(name, WRITERS)]
        aliases = imported_as(tree)
        handed_over = _writing_files(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = dotted(func)
            root, dot, rest = name.partition(".")
            name = aliases.get(root, root) + dot + rest
            if isinstance(func, ast.Attribute) and func.attr in (
                "write_bytes",
                "write_text",
                "rename",
            ):
                found.append(f"{path}:{node.lineno} {func.attr}")
            elif (name == "open" or name.endswith(".open")) and _writes(node):
                into = node.args[0] if node.args else None
                if name == "wave.open" and isinstance(into, ast.Name) and into.id in handed_over:
                    continue
                found.append(f"{path}:{node.lineno} {name}")
    return found


RULES: dict[str, Callable[[list[Source]], list[str]]] = {
    rule.__name__: rule
    for rule in [
        only_db_commits,
        settings_only_through_get_settings,
        external_sdks_only_in_clients,
        subprocesses_only_in_audio,
        routers_stay_thin,
        no_sleeps_or_retry_loops_outside_worker,
        no_session_level_set,
        get_session_only_through_db_session,
        visit_status_only_through_set_status,
        routers_carry_their_own_prefix,
        job_times_come_from_postgres,
        clients_import_only_the_allowlist,
        jobs_imports_no_domain_module,
        files_written_only_by_storage,
    ]
}

BAD_EXAMPLES = [
    (
        "files_written_only_by_storage",
        "audio.py",
        "def f(path):\n    with path.open('wb') as out:\n        pass\n",
    ),
    ("files_written_only_by_storage", "audio.py", "with open(name, 'a') as log:\n    pass\n"),
    ("files_written_only_by_storage", "audio.py", "with open(name, mode='w+') as log:\n    pass\n"),
    ("files_written_only_by_storage", "transcription.py", "path.write_text('{}')\n"),
    ("files_written_only_by_storage", "transcription.py", "path.write_bytes(b'')\n"),
    ("files_written_only_by_storage", "audio.py", "import os\nos.replace(a, b)\n"),
    ("files_written_only_by_storage", "audio.py", "from os import rename\nrename(a, b)\n"),
    ("files_written_only_by_storage", "audio.py", "import wave\nwave.open(str(path), 'wb')\n"),
    ("files_written_only_by_storage", "audio.py", "import wave as w\nw.open(str(path), 'wb')\n"),
    (
        "files_written_only_by_storage",
        "audio.py",
        "import wave\nname = str(p)\nwave.open(name, 'wb')\n",
    ),
    ("files_written_only_by_storage", "audio.py", "import gzip\ngzip.open(path, 'wb')\n"),
    ("files_written_only_by_storage", "audio.py", "import shutil\nshutil.move(a, b)\n"),
    ("files_written_only_by_storage", "audio.py", "from shutil import copyfile\ncopyfile(a, b)\n"),
    ("files_written_only_by_storage", "audio.py", "import os\nos.fdopen(fd, 'wb')\n"),
    ("files_written_only_by_storage", "audio.py", "part.rename(target)\n"),
    ("clients_import_only_the_allowlist", "stt/meta.py", "from countertalk import visits\n"),
    (
        "clients_import_only_the_allowlist",
        "stt/meta.py",
        "from countertalk.db import session_scope\n",
    ),
    ("clients_import_only_the_allowlist", "llm/client.py", "import sqlalchemy\n"),
    ("clients_import_only_the_allowlist", "stt/meta.py", "from countertalk import storage\n"),
    ("clients_import_only_the_allowlist", "stt/meta.py", "from .. import db\n"),
    ("clients_import_only_the_allowlist", "llm/client.py", "from countertalk.llm_calls import x\n"),
    ("jobs_imports_no_domain_module", "jobs.py", "from . import visits\n"),
    ("jobs_imports_no_domain_module", "jobs.py", "from .visits import fail_processing\n"),
    ("jobs_imports_no_domain_module", "jobs.py", "from countertalk import limits, visits\n"),
    (
        "jobs_imports_no_domain_module",
        "jobs.py",
        "from countertalk.visits import fail_processing\n",
    ),
    ("job_times_come_from_postgres", "jobs.py", "from countertalk.models import utc_now\n"),
    ("job_times_come_from_postgres", "jobs.py", "from datetime import datetime\ndatetime.now()\n"),
    ("job_times_come_from_postgres", "jobs.py", "import time\ntime.time()\n"),
    ("job_times_come_from_postgres", "jobs.py", "from time import time_ns\n"),
    ("job_times_come_from_postgres", "jobs.py", "import datetime\ndatetime.date.today()\n"),
    (
        "job_times_come_from_postgres",
        "jobs.py",
        "from datetime import datetime as dt\ndt.utcnow()\n",
    ),
    ("routers_carry_their_own_prefix", "main.py", "app.include_router(r, prefix='/api/x')\n"),
    ("routers_carry_their_own_prefix", "api/visits.py", "router = APIRouter(tags=['visits'])\n"),
    ("only_db_commits", "visits.py", "def f(session):\n    session.commit()\n"),
    ("settings_only_through_get_settings", "visits.py", "import os\nKEY = os.environ['K']\n"),
    ("settings_only_through_get_settings", "jobs.py", "import os as o\nKEY = o.environ['K']\n"),
    ("settings_only_through_get_settings", "jobs.py", "from os import getenv\n"),
    (
        "settings_only_through_get_settings",
        "jobs.py",
        "from countertalk import config\nconfig.Settings()\n",
    ),
    ("external_sdks_only_in_clients", "visits.py", "import httpx\n"),
    ("external_sdks_only_in_clients", "api/visits.py", "from openai import OpenAI\n"),
    ("external_sdks_only_in_clients", "jobs.py", "import websockets.sync.client\n"),
    ("subprocesses_only_in_audio", "jobs.py", "import subprocess\n"),
    ("subprocesses_only_in_audio", "jobs.py", "import os\nos.system('ffmpeg -version')\n"),
    ("subprocesses_only_in_audio", "jobs.py", "import os\nos.execvp('ffmpeg', [])\n"),
    ("routers_stay_thin", "api/visits.py", "from sqlalchemy import select\n"),
    ("routers_stay_thin", "api/visits.py", "def f(session):\n    session.execute(q)\n"),
    ("routers_stay_thin", "api/visits.py", "def f(db: DbSession):\n    db.add(visit)\n"),
    ("no_sleeps_or_retry_loops_outside_worker", "jobs.py", "import time\ntime.sleep(1)\n"),
    ("no_sleeps_or_retry_loops_outside_worker", "jobs.py", "import time as t\nt.sleep(1)\n"),
    ("no_sleeps_or_retry_loops_outside_worker", "stt/meta.py", "from time import sleep\n"),
    ("no_sleeps_or_retry_loops_outside_worker", "jobs.py", "while True:\n    pass\n"),
    ("no_sleeps_or_retry_loops_outside_worker", "llm/client.py", "import tenacity\n"),
    ("no_session_level_set", "jobs.py", "session.execute(text('SET search_path TO other'))\n"),
    ("no_session_level_set", "db.py", "conn.exec_driver_sql('  set time zone UTC')\n"),
    ("no_session_level_set", "jobs.py", "text(f'SET statement_timeout = {ms}')\n"),
    (
        "get_session_only_through_db_session",
        "api/visits.py",
        "from countertalk.db import get_session\n",
    ),
    (
        "visit_status_only_through_set_status",
        "jobs.py",
        "def f(visit):\n    visit.status = 'failed'\n",
    ),
    (
        "visit_status_only_through_set_status",
        "visits.py",
        "def fail(job):\n    job.visit.status = 'x'\n",
    ),
]

ALLOWED_EXAMPLES = [
    (
        "files_written_only_by_storage",
        "storage.py",
        "import os\nos.replace(a, b)\npath.open('xb')\n",
    ),
    ("files_written_only_by_storage", "audio.py", "with part.open('rb') as piece:\n    pass\n"),
    ("files_written_only_by_storage", "audio.py", "with open(name) as f:\n    pass\n"),
    ("files_written_only_by_storage", "audio.py", "import wave\nwave.open(str(path), 'rb')\n"),
    (
        "files_written_only_by_storage",
        "audio.py",
        "from PIL import Image\nImage.open('talk.wav')\n",
    ),
    ("files_written_only_by_storage", "audio.py", "text.replace('a', 'b')\n"),
    (
        "files_written_only_by_storage",
        "audio.py",
        "import wave\nwith storage.writing(p) as out:\n    wave.open(out, 'wb')\n",
    ),
    ("clients_import_only_the_allowlist", "stt/meta.py", "import json\nimport httpx\n"),
    (
        "clients_import_only_the_allowlist",
        "stt/meta.py",
        "from countertalk import limits\nfrom countertalk.errors import PermanentError\n",
    ),
    (
        "clients_import_only_the_allowlist",
        "stt/meta.py",
        "from countertalk.stt.base import Reply\n",
    ),
    ("clients_import_only_the_allowlist", "visits.py", "from countertalk import storage\n"),
    ("clients_import_only_the_allowlist", "stt/meta.py", "from .base import Reply\n"),
    ("clients_import_only_the_allowlist", "stt/meta.py", "import websockets\n"),
    ("jobs_imports_no_domain_module", "jobs.py", "from countertalk import limits\n"),
    ("jobs_imports_no_domain_module", "worker.py", "from countertalk import visits\n"),
    ("job_times_come_from_postgres", "jobs.py", "from sqlalchemy import func\nfunc.now()\n"),
    ("job_times_come_from_postgres", "visits.py", "from countertalk.models import utc_now\n"),
    ("routers_carry_their_own_prefix", "main.py", "app.include_router(visits.router)\n"),
    (
        "routers_carry_their_own_prefix",
        "api/visits.py",
        "router = APIRouter(prefix='/api/visits')\n",
    ),
    ("only_db_commits", "db.py", "def f(session):\n    session.commit()\n"),
    (
        "settings_only_through_get_settings",
        "config.py",
        "from pydantic_settings import BaseSettings\n",
    ),
    ("external_sdks_only_in_clients", "stt/meta.py", "import httpx\n"),
    ("external_sdks_only_in_clients", "llm/client.py", "from openai import OpenAI\n"),
    ("subprocesses_only_in_audio", "audio.py", "import subprocess\n"),
    ("routers_stay_thin", "api/visits.py", "from sqlalchemy.orm import Session\n"),
    (
        "routers_stay_thin",
        "api/visits.py",
        "def f(headers: dict):\n    headers.get('X-Content-SHA256')\n",
    ),
    (
        "no_sleeps_or_retry_loops_outside_worker",
        "worker.py",
        "import time\nwhile True:\n    time.sleep(1)\n",
    ),
    ("no_session_level_set", "db.py", "conn.exec_driver_sql('SET TRANSACTION READ ONLY')\n"),
    ("no_session_level_set", "jobs.py", "text('SET LOCAL lock_timeout = 1000')\n"),
    ("no_session_level_set", "jobs.py", "text(\"UPDATE jobs SET state = 'done'\")\n"),
    (
        "no_session_level_set",
        "visits.py",
        "def set_status(visit, new):\n    '''Set the status.'''\n",
    ),
    ("no_session_level_set", "jobs.py", "log.warning('Set MODEL_API_KEY in backend/.env')\n"),
    (
        "get_session_only_through_db_session",
        "api/visits.py",
        "from countertalk.db import DbSession\n",
    ),
    (
        "visit_status_only_through_set_status",
        "visits.py",
        "def set_status(visit, new):\n    visit.status = new\n",
    ),
    (
        "visit_status_only_through_set_status",
        "inbox.py",
        "def f(insight):\n    insight.status = 'done'\n",
    ),
]


@pytest.mark.parametrize("rule", RULES)
def test_app_code_follows_the_rule(rule: str) -> None:
    assert RULES[rule](app_sources()) == []


@pytest.mark.parametrize(("rule", "path", "code"), BAD_EXAMPLES)
def test_rule_catches_a_bad_example(rule: str, path: str, code: str) -> None:
    assert RULES[rule]([(path, ast.parse(code))]) != []


@pytest.mark.parametrize(("rule", "path", "code"), ALLOWED_EXAMPLES)
def test_rule_allows_what_the_guidelines_allow(rule: str, path: str, code: str) -> None:
    assert RULES[rule]([(path, ast.parse(code))]) == []


def test_every_rule_has_a_bad_example() -> None:
    assert {rule for rule, _, _ in BAD_EXAMPLES} == set(RULES)


def test_sarvam_stays_out_of_the_app() -> None:
    """BRIEF D15: Sarvam makes test audio only (scripts/make_test_audio.py); the app never calls it.

    The source text, not the syntax tree, so a comment or docstring counts too.
    """
    mentions = [
        f"{path.relative_to(APP_DIR).as_posix()}:{number}"
        for path in sorted(APP_DIR.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if "sarvam" in line.lower()
    ]

    assert mentions == []
