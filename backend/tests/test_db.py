import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError, InternalError
from sqlalchemy.orm import Session

from countertalk.db import database_reachable, get_engine, session_scope
from countertalk.errors import Conflict
from countertalk.limits import IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS, LOCK_TIMEOUT_MS
from countertalk.models import Org
from tests.conftest import PINNED_SETTINGS, org_count, terminate_backends

READ_ONLY_SQL_TRANSACTION = "25006"


def setting_ms(name: str) -> int:
    """A Postgres setting on a brand-new connection, in milliseconds (what SHOW reports)."""
    get_engine().dispose()
    with get_engine().connect() as conn:
        value, unit = conn.execute(
            text("SELECT setting, unit FROM pg_settings WHERE name = :name"), {"name": name}
        ).one()
    assert unit == "ms"
    return int(value)


@pytest.mark.parametrize(
    ("name", "expected_ms"),
    [
        ("lock_timeout", LOCK_TIMEOUT_MS),
        ("idle_in_transaction_session_timeout", IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS),
    ],
)
def test_a_new_connection_sets_the_safety_timeouts(name: str, expected_ms: int) -> None:
    assert setting_ms(name) == expected_ms


def test_safety_timeouts_survive_a_rollback() -> None:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
        conn.rollback()
        lock_timeout = conn.scalar(
            text("SELECT setting FROM pg_settings WHERE name = 'lock_timeout'")
        )

    assert lock_timeout == str(LOCK_TIMEOUT_MS)


def test_session_scope_commits_when_the_block_finishes() -> None:
    with session_scope() as session:
        session.add(Org(name="Committed Org"))

    assert org_count() == 1


def test_session_scope_rolls_back_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError), session_scope() as session:
        session.add(Org(name="Rolled Back Org"))
        session.flush()
        raise RuntimeError("fails after the write")

    assert org_count() == 0


def test_read_only_session_scope_refuses_a_write() -> None:
    with pytest.raises(InternalError) as error, session_scope(read_only=True) as session:
        session.add(Org(name="Written In Read Only"))
        session.flush()

    assert error.value.orig is not None
    assert getattr(error.value.orig, "sqlstate", None) == READ_ONLY_SQL_TRANSACTION
    assert org_count() == 0


def test_read_only_session_scope_stays_read_only_after_a_rollback() -> None:
    with pytest.raises(InternalError), session_scope(read_only=True) as session:
        session.execute(select(1))
        session.rollback()
        session.add(Org(name="Written After Rollback"))
        session.flush()

    assert org_count() == 0


def test_rollback_on_a_dropped_connection_keeps_the_original_error() -> None:
    with pytest.raises(Conflict), session_scope() as session:
        session.add(Org(name="Dropped Org"))
        session.flush()
        terminate_backends(session.scalar(text("SELECT pg_backend_pid()")))
        raise Conflict("test_conflict", "The error the caller must see")

    assert org_count() == 0


def test_database_reachable_is_false_when_no_connection_is_free() -> None:
    engine = create_engine(
        PINNED_SETTINGS["DATABASE_URL"], pool_size=1, max_overflow=0, pool_timeout=0.1
    )
    try:
        with engine.connect(), Session(engine) as session:
            assert database_reachable(session) is False
    finally:
        engine.dispose()


def test_database_errors_do_not_show_the_sql_parameters() -> None:
    with session_scope() as session:
        session.add(Org(name="Same Name"))

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(Org(name="Same Name"))

    assert "[parameters:" not in str(error.value)
    assert "SQL parameters hidden" in str(error.value)
