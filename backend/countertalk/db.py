"""The engine and sessions: the only place that commits (BRIEF §7, CODING_GUIDELINES §2.3)."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from pydantic import ValidationError
from sqlalchemy import Connection, Engine, create_engine, event, select
from sqlalchemy.exc import ArgumentError, DBAPIError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, SessionTransaction

from countertalk.config import get_settings
from countertalk.limits import (
    DB_CONNECT_TIMEOUT_MS,
    IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS,
    LOCK_TIMEOUT_MS,
)

log = logging.getLogger(__name__)

READ_ONLY_METHODS = frozenset({"GET", "HEAD"})


@lru_cache
def _engine_for(url: str) -> Engine:
    # Startup options apply to every connection before its first query and, unlike SET,
    # can't be undone by a rollback.
    options = (
        f"-c lock_timeout={LOCK_TIMEOUT_MS}"
        f" -c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS}"
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        # Errors and logs leave out the values sent, which can be personal data (§1.9).
        hide_parameters=True,
        # libpq takes whole seconds.
        connect_args={"options": options, "connect_timeout": DB_CONNECT_TIMEOUT_MS // 1000},
    )


def get_engine() -> Engine:
    return _engine_for(get_settings().DATABASE_URL)


def _set_read_only(session: Session, transaction: SessionTransaction, conn: Connection) -> None:
    # One snapshot for the whole transaction (batch 14): a page read in several statements,
    # like the review's, never mixes rows from before and after another's commit. A read-only
    # transaction never fails to serialise.
    conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")


@contextmanager
def session_scope(*, read_only: bool = False) -> Iterator[Session]:
    """One unit of work: committed when the block finishes, rolled back if it raises."""
    session = Session(get_engine(), expire_on_commit=False)
    if read_only:
        # Runs first in every transaction this session begins, including after a rollback.
        event.listen(session, "after_begin", _set_read_only)
    try:
        yield session
        session.commit()
    except BaseException:
        _rollback_quietly(session)
        raise
    finally:
        session.close()


def _rollback_quietly(session: Session) -> None:
    """Rolls back, without letting a dead connection replace the error that caused it."""
    try:
        session.rollback()
    except SQLAlchemyError as error:
        log.warning("rollback failed error=%s", type(error).__name__)


def get_session(request: Request) -> Iterator[Session]:
    """The request's one transaction. GET and HEAD can only read."""
    with session_scope(read_only=request.method in READ_ONLY_METHODS) as session:
        yield session


# Routes take `session: DbSession`. scope="function" commits before the response is sent, so a
# commit that fails reaches the client as an error, never as a success.
DbSession = Annotated[Session, Depends(get_session, scope="function")]


def database_reachable(session: Session) -> bool:
    """False if Postgres can't answer SELECT 1: it's down, or no pooled connection is free."""
    try:
        session.execute(select(1))
    except (DBAPIError, PoolTimeoutError) as error:
        log.warning("database unreachable error=%s", type(error).__name__)
        _rollback_quietly(session)
        return False
    return True


def constraint_name(error: BaseException) -> str | None:
    """The constraint a database error names, if any: matched on, never its message (§1.9)."""
    diag = getattr(getattr(error, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None)


# What a dev script catches around its database work, to say in one line what went wrong.
SETUP_ERRORS = (ValidationError, ArgumentError, DBAPIError)


def setup_trouble(error: BaseException) -> str:
    """One line for a settings or database failure, never the driver's text (it can quote the
    URL and its password)."""
    if isinstance(error, ValidationError | ArgumentError):
        return "the settings aren't valid; run scripts/check_setup.py"
    if isinstance(error, OperationalError):
        return (
            f"the database didn't answer ({type(error.orig).__name__}); run scripts/check_setup.py"
        )
    if isinstance(error, DBAPIError):
        # It answered, and refused: most often a database that isn't migrated yet.
        return (
            f"the database refused it ({type(error.orig).__name__});"
            " is it migrated? run uv run alembic upgrade head"
        )
    return type(error).__name__
