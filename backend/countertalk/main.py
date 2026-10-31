"""The app: routers, and the handlers that give every error the API's one shape (§2.10).

{"error": {"code", "message", "details"?}}: domain errors carry their own code; the framework's
own errors (unknown route, wrong method, wrong request shape) and unexpected ones get one here.
"""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from countertalk.api import health
from countertalk.errors import DomainError


def _error_response(
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return JSONResponse({"error": body}, status_code=status, headers=headers)


async def _domain_error(request: Request, error: DomainError) -> JSONResponse:
    return _error_response(error.http_status, error.code, error.message, error.details)


async def _http_error(request: Request, error: HTTPException) -> JSONResponse:
    phrase = HTTPStatus(error.status_code).phrase
    code = phrase.lower().replace(" ", "_").replace("-", "_")  # "Not Found" -> "not_found"
    return _error_response(error.status_code, code, phrase, headers=error.headers)


async def _invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
    # Where and why only: the submitted values aren't echoed back.
    problems = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in error.errors()]
    return _error_response(
        HTTPStatus.UNPROCESSABLE_ENTITY,
        "invalid_request",
        "The request doesn't match what the API expects",
        details={"errors": problems},
    )


async def _unexpected_error(request: Request, error: Exception) -> JSONResponse:
    # The server still logs the traceback; the client never sees the exception's text.
    return _error_response(
        HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Something went wrong on the server"
    )


def create_app() -> FastAPI:
    app = FastAPI(title="CounterTalk")
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_exception_handler(Exception, _unexpected_error)
    app.include_router(health.router, prefix="/api/health")
    return app


app = create_app()
