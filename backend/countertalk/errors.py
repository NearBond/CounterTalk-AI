"""Domain errors (CODING_GUIDELINES §2.10).

`code` is snake_case and never changes once released, because clients rely on it. `message` is
for people. main.py turns these into {"error": {"code", "message", "details"?}}.
"""

from typing import Any, ClassVar


class DomainError(Exception):
    http_status: ClassVar[int] = 500

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


class Invalid(DomainError):
    http_status = 400


class SignedOut(DomainError):
    """No valid session (batch 16): signed out, expired, or a cookie that isn't ours. The web
    app goes to /signin."""

    http_status = 401


class NotFound(DomainError):
    """Missing, or belongs to another org: never 403, which would reveal that it exists."""

    http_status = 404


class Forbidden(DomainError):
    """Not this user's to do, on something they can see: changing the due date of an issue
    someone else owns, say (batch 15)."""

    http_status = 403


class Conflict(DomainError):
    """Wrong state, or conflicting data."""

    http_status = 409


class TooMany(DomainError):
    """Too many tries for now (batch 16's sign-in): 429, with when to try again."""

    http_status = 429

    def __init__(self, code: str, message: str, *, retry_after_s: int) -> None:
        super().__init__(code, message)
        self.retry_after_s = retry_after_s


class TooLarge(DomainError):
    """A body over its limit, e.g. a segment over MAX_SEGMENT_BYTES."""

    http_status = 413


class LengthRequired(DomainError):
    """A body sent without Content-Length, which an upload limit needs before reading it."""

    http_status = 411


class PermanentError(DomainError):
    """Work that retrying can't fix: an unreadable file, or a 4xx from an external service.

    Job handlers and client modules raise it, and the job fails at once (§9.5). A request's own
    bad input is Invalid (400), not this.
    """

    http_status = 500


class RetryableError(DomainError):
    """A passing failure: a network error, 429, 5xx or a lock timeout. A job retries later."""

    http_status = 503


def claim_lost() -> RetryableError:
    """A handler that pays per call stops once its claim is lost (§2.4): another worker may
    have the job now."""
    return RetryableError(
        "claim_lost", "This worker lost the job, so it stopped before paying for more"
    )


def describe(error: BaseException) -> str:
    """`code: message`, for failure reasons and logs.

    Other exceptions give only their type: their text can hold personal data (§1.9).
    """
    if isinstance(error, DomainError):
        return f"{error.code}: {error.message}"
    return f"unexpected_error: {type(error).__name__}"
