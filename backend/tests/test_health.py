import logging

import pytest
from fastapi.testclient import TestClient

from countertalk.config import get_settings
from tests.conftest import SERVER_URL, terminate_backends


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_reports_unreachable_database_with_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A missing database fails at connect at once, through the same path as a server that's
    # down; a closed port would wait out the whole connect timeout on Windows.
    monkeypatch.setenv("DATABASE_URL", f"{SERVER_URL}/countertalk_missing")
    get_settings.cache_clear()

    with caplog.at_level(logging.WARNING):
        response = client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "database": "error"}
    assert "countertalk:countertalk" not in caplog.text


def test_health_survives_a_connection_the_server_dropped(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200
    terminate_backends()

    response = client.get("/api/health")

    assert response.json() == {"status": "ok", "database": "ok"}
