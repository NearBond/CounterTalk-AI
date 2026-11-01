import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from countertalk.errors import (
    Conflict,
    DomainError,
    Invalid,
    NotFound,
    PermanentError,
    RetryableError,
    describe,
)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (Invalid("bad_seq", "seq must be 0 or more"), 400),
        (NotFound("visit_not_found", "No such visit"), 404),
        (Conflict("visit_not_recording", "The visit isn't recording"), 409),
        (PermanentError("audio_unreadable", "The audio can't be read"), 500),
        (RetryableError("database_busy", "Try again shortly"), 503),
    ],
)
def test_domain_error_becomes_the_error_shape(
    app: FastAPI, client: TestClient, error: DomainError, status: int
) -> None:
    @app.post("/api/test/fail")
    def fail() -> None:
        raise error

    response = client.post("/api/test/fail")

    assert response.status_code == status
    assert response.json() == {"error": {"code": error.code, "message": error.message}}


def test_error_details_are_included_when_given(app: FastAPI, client: TestClient) -> None:
    @app.post("/api/test/fail")
    def fail() -> None:
        raise Conflict(
            "segments_missing", "Some segments are missing", details={"missing_seqs": [1, 3]}
        )

    response = client.post("/api/test/fail")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "segments_missing",
            "message": "Some segments are missing",
            "details": {"missing_seqs": [1, 3]},
        }
    }


def test_describe_gives_code_and_message() -> None:
    error = NotFound("visit_not_found", "No such visit")

    assert describe(error) == "visit_not_found: No such visit"
    assert str(error) == "visit_not_found: No such visit"


def test_describe_hides_the_text_of_unexpected_errors() -> None:
    error = ValueError("Test Shopkeeper said the credit note is pending")

    assert describe(error) == "unexpected_error: ValueError"


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/api/no-such-thing", 404, "not_found"),
        ("DELETE", "/api/health", 405, "method_not_allowed"),
    ],
)
def test_the_frameworks_own_errors_use_the_error_shape(
    client: TestClient, method: str, path: str, status: int, code: str
) -> None:
    response = client.request(method, path)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_request_of_the_wrong_shape_uses_the_error_shape(app: FastAPI, client: TestClient) -> None:
    @app.post("/api/test/items/{item_id}")
    def create(item_id: int) -> None:
        pass

    response = client.post("/api/test/items/not-a-number")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    [problem] = error["details"]["errors"]
    # Where and why only: the submitted value isn't echoed back.
    assert set(problem) == {"loc", "msg", "type"}
    assert problem["loc"] == ["path", "item_id"]
    assert problem["type"] == "int_parsing"


def test_unexpected_exception_uses_the_error_shape_without_its_text(
    app: FastAPI, client: TestClient
) -> None:
    @app.post("/api/test/crash")
    def crash() -> None:
        raise RuntimeError("Test Shopkeeper's private words")

    response = client.post("/api/test/crash")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private words" not in response.text
