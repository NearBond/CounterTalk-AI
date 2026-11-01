"""Logs hold no personal data (CODING_GUIDELINES §1.9, batch 2 decision 7)."""

import asyncio
import io
import logging
import re
import socket
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from countertalk import visits
from countertalk.db import DbSession, session_scope
from countertalk.logs import HANDLER_NAME, configure_logging
from countertalk.main import create_app
from countertalk.models import Org, Role, User, new_id
from scripts.seed_dev import seed

FAKE_EMAIL = "leaky.person@example.com"
FAKE_NAME = "Leaky Person"
NO_SHOP_MESSAGE = f"no shop for {FAKE_EMAIL}"
DETAIL_MESSAGE = f"Key (email)=({FAKE_EMAIL}) already exists"
SERVER_START_TIMEOUT_S = 10


@contextmanager
def served(app: FastAPI) -> Iterator[str]:
    """Runs the app under a real uvicorn, which is what logs an escaped exception's traceback."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("uvicorn didn't start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=SERVER_START_TIMEOUT_S)


def app_with_a_duplicate_email_route() -> FastAPI:
    with session_scope() as session:
        org = Org(name="Log Test Org")
        session.add(org)
        session.flush()
        session.add(User(org_id=org.id, email=FAKE_EMAIL, name=FAKE_NAME, role=Role.REP))

    app = create_app()

    @app.post("/api/test/users")
    def add_the_same_email_again(session: DbSession) -> None:
        org_id = session.scalars(select(Org.id)).one()
        session.add(User(org_id=org_id, email=FAKE_EMAIL, name=FAKE_NAME, role=Role.REP))
        session.flush()

    return app


def test_a_unique_violation_logs_no_part_of_the_email(caplog: pytest.LogCaptureFixture) -> None:
    app = app_with_a_duplicate_email_route()

    with caplog.at_level(logging.DEBUG), served(app) as base:
        response = httpx.post(f"{base}/api/test/users", timeout=SERVER_START_TIMEOUT_S)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    logs = caplog.text.lower()
    for part in ("leaky", "leaky.person", "example.com", FAKE_EMAIL, FAKE_NAME):
        assert part.lower() not in logs
    # "person" as a whole word: the withheld-message note itself says "personal data".
    assert re.search(r"person", logs) is None
    assert "method=post route=/api/test/users" in logs
    assert "error=integrityerror constraint=uq_users_email sqlstate=23505" in logs
    # The frames are there, down to the route that failed, with every message withheld.
    assert "traceback (most recent call last)" in logs
    assert "add_the_same_email_again" in logs
    assert "message not logged" in logs


def test_an_error_after_the_response_started_is_logged_without_a_second_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app()

    @app.get("/api/test/stream")
    def stream() -> StreamingResponse:
        def chunks() -> Iterator[bytes]:
            yield b"first part"
            raise RuntimeError(f"failed while sending {FAKE_EMAIL}")

        return StreamingResponse(chunks())

    with caplog.at_level(logging.DEBUG), served(app) as base:
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"{base}/api/test/stream", timeout=SERVER_START_TIMEOUT_S).read()

    assert "route=/api/test/stream error=runtimeerror" in caplog.text.lower()
    assert "leaky" not in caplog.text.lower()


def test_logging_is_set_up_once_for_the_app_and_uvicorn() -> None:
    root = logging.getLogger()
    # configure_logging replaces the root handlers, pytest's own among them: put them back after.
    saved = (list(root.handlers), root.level)
    try:
        configure_logging()
        configure_logging()

        ours = [h for h in root.handlers if h.get_name() == HANDLER_NAME]
        assert len(ours) == 1
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logger = logging.getLogger(name)
            assert logger.handlers == []
            assert logger.propagate is True
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved[0]:
            root.addHandler(handler)
        root.setLevel(saved[1])
    assert root.handlers == saved[0]


def test_a_logged_traceback_keeps_its_frames_but_no_message() -> None:
    # Frames show their source lines, so the messages come from constants defined outside them,
    # as data does in real code.
    def save_user() -> None:
        try:
            raise LookupError(NO_SHOP_MESSAGE)
        except LookupError as cause:
            raise ValueError(DETAIL_MESSAGE) from cause

    try:
        save_user()
    except ValueError:
        record = logging.LogRecord(
            "countertalk.test", logging.ERROR, __file__, 1, "failed", (), None
        )
        record.exc_info = sys.exc_info()
    handler = next(h for h in logging.getLogger().handlers if h.get_name() == HANDLER_NAME)

    text = handler.format(record)

    assert "save_user" in text
    assert "ValueError" in text
    assert "LookupError" in text
    assert "leaky" not in text.lower()
    assert "already exists" not in text


def test_a_failure_while_sending_the_500_carries_no_trace_of_the_first_error() -> None:
    app = create_app()

    @app.get("/api/test/fail")
    def fail() -> None:
        raise RuntimeError(f"Key (email)=({FAKE_EMAIL}) already exists")

    async def call() -> None:
        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            raise OSError("the connection is gone")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/test/fail",
            "raw_path": b"/api/test/fail",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)

    with pytest.raises(OSError) as raised:
        asyncio.run(call())

    assert "leaky" not in "".join(traceback.format_exception(raised.value)).lower()


def test_access_log_lines_leave_out_the_query_string() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50000", "GET", "/api/visits?shop=Leaky%20Person", "1.1", 200),
        None,
    )

    assert logging.getLogger("uvicorn.access").filter(record)

    assert record.getMessage() == '127.0.0.1:50000 - "GET /api/visits HTTP/1.1" 200'


def test_any_handler_gets_the_frames_but_never_the_message() -> None:
    # A handler that isn't ours, with the standard formatter: an error tracker, or a
    # --log-config uvicorn. The record itself carries only the frames.
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("countertalk.test.other_handler")
    logger.addHandler(handler)
    try:
        try:
            raise ValueError(DETAIL_MESSAGE)
        except ValueError as error:
            logger.error("failed", exc_info=error)
    finally:
        logger.removeHandler(handler)

    text = out.getvalue()
    assert "Traceback (most recent call last)" in text
    assert "ValueError" in text
    assert "leaky" not in text.lower()


def test_a_500_in_an_included_router_logs_the_routes_whole_template(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    with session_scope() as session:
        seed(session)

    def broken(*args: object) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(visits, "get_visit", broken)

    with caplog.at_level(logging.ERROR):
        response = client.get(f"/api/visits/{new_id()}")

    assert response.status_code == 500
    # The template, prefix and all, never the path with its IDs.
    assert "route=/api/visits/{visit_id} " in caplog.text
