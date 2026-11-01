"""The app: routers, the built web app, and the handlers that give every error one shape (§2.10).

{"error": {"code", "message", "details"?}}: domain errors carry their own code; the framework's
own errors (unknown route, wrong method, wrong request shape) and unexpected ones get one here.
"""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from countertalk.api import debug, health, shops, visits
from countertalk.config import get_settings
from countertalk.db import constraint_name
from countertalk.errors import DomainError
from countertalk.logs import configure_logging
from countertalk.web import mount_web_app

log = logging.getLogger(__name__)


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


def _sqlstate(error: BaseException) -> str:
    return getattr(getattr(error, "orig", None), "sqlstate", None) or "-"


class UnexpectedErrors:
    """Turns any exception that escapes a request into the 500 error shape, and logs it safely.

    It has to be a middleware: an `Exception` handler isn't enough, because Starlette's
    ServerErrorMiddleware runs the handler and then re-raises, and uvicorn logs the traceback
    with every message in it. psycopg's includes Postgres's DETAIL, e.g. `Key (email)=(...)`
    (§1.9). So the exception stops here. The log line holds its type, the constraint name, the
    SQLSTATE, and the request's method and route template (never the path itself), plus the
    traceback, whose messages the formatter withholds (§2.8).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_and_note(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, send_and_note)
            return
        except Exception as error:  # noqa: BLE001 - every escaped error ends here, logged safely
            route = getattr(scope.get("route"), "path", "-")
            log.error(
                "request failed method=%s route=%s error=%s constraint=%s sqlstate=%s",
                scope["method"],
                route,
                type(error).__name__,
                constraint_name(error) or "-",
                _sqlstate(error),
                exc_info=error,
            )
        if started:
            # Part of the response is already out; the server closes the connection.
            return
        # Sent outside the except block: if sending fails too, that new error mustn't carry the
        # first one, and its message, along as its context.
        response = _error_response(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "internal_error",
            "Something went wrong on the server",
        )
        await response(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(title="CounterTalk")
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_middleware(UnexpectedErrors)
    # Each router carries its own /api/<area> prefix, so a route's template (logged when it
    # fails) is the whole path: an include_router prefix isn't part of route.path.
    app.include_router(health.router)
    app.include_router(shops.router)
    app.include_router(visits.router)
    if get_settings().DEBUG:
        # Test hooks (queueing test jobs): never on a deployed service, where DEBUG=0.
        app.include_router(debug.router)
    # Last, so every API route is tried first.
    mount_web_app(app, get_settings().WEB_DIST_DIR)
    return app


configure_logging()
app = create_app()
