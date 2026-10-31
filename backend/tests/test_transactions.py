"""A request is one transaction, and GET and HEAD can only read (CODING_GUIDELINES §2.3)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from countertalk.db import DbSession, session_scope
from countertalk.errors import Conflict
from countertalk.models import Org
from tests.conftest import org_count


def test_route_that_writes_and_returns_commits(app: FastAPI, client: TestClient) -> None:
    @app.post("/api/test/orgs")
    def create(session: DbSession) -> dict[str, bool]:
        session.add(Org(name="New Org"))
        return {"ok": True}

    response = client.post("/api/test/orgs")

    assert response.status_code == 200
    assert org_count() == 1


def test_route_that_writes_then_raises_leaves_nothing(app: FastAPI, client: TestClient) -> None:
    @app.post("/api/test/orgs")
    def create(session: DbSession) -> dict[str, bool]:
        session.add(Org(name="Half Done Org"))
        session.flush()
        raise Conflict("test_conflict", "Refused after the write")

    response = client.post("/api/test/orgs")

    assert response.status_code == 409
    assert org_count() == 0


def test_route_that_crashes_after_a_write_leaves_nothing(app: FastAPI, client: TestClient) -> None:
    @app.post("/api/test/orgs")
    def create(session: DbSession) -> dict[str, bool]:
        session.add(Org(name="Crashed Org"))
        session.flush()
        raise RuntimeError("a bug after the write")

    response = client.post("/api/test/orgs")

    assert response.status_code == 500
    assert org_count() == 0


def test_commit_that_fails_is_not_reported_as_success(app: FastAPI, client: TestClient) -> None:
    with session_scope() as session:
        session.add(Org(name="Taken Name"))

    @app.post("/api/test/orgs")
    def create(session: DbSession) -> dict[str, bool]:
        # Not flushed here: the duplicate is only found when the dependency commits.
        session.add(Org(name="Taken Name"))
        return {"ok": True}

    response = client.post("/api/test/orgs")

    assert response.status_code == 500
    assert org_count() == 1


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_write_inside_a_read_request_fails(app: FastAPI, client: TestClient, method: str) -> None:
    @app.api_route("/api/test/orgs", methods=[method])
    def create(session: DbSession) -> dict[str, bool]:
        session.add(Org(name="Written In A Read"))
        session.flush()
        return {"ok": True}

    response = client.request(method, "/api/test/orgs")

    assert response.status_code == 500
    assert org_count() == 0
