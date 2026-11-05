"""/api/debug/jobs: queues a test job, and exists only when DEBUG=1 (batch 4)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from countertalk.config import get_settings
from countertalk.main import create_app
from countertalk.models import JobState
from tests.test_jobs import job_row


@pytest.fixture
def debug_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DEBUG", "1")
    get_settings.cache_clear()
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def test_without_debug_there_is_no_test_jobs_endpoint(client: TestClient) -> None:
    response = client.post("/api/debug/jobs", json={"kind": "ping"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("kind", ["ping", "sleep"])
def test_with_debug_a_test_job_is_queued(debug_client: TestClient, kind: str) -> None:
    response = debug_client.post("/api/debug/jobs", json={"kind": kind})

    assert response.status_code == 201
    body = response.json()
    assert body["kind"] == kind
    assert job_row(body["id"]).state == JobState.QUEUED


def test_only_a_test_job_can_be_queued_there(debug_client: TestClient) -> None:
    response = debug_client.post("/api/debug/jobs", json={"kind": "transcribe"})

    assert response.status_code == 422
