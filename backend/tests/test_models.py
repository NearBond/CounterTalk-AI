import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, StatementError

from countertalk.db import session_scope
from countertalk.models import Org, Role, Shop, User, new_id, utc_now

IST = timezone(timedelta(hours=5, minutes=30))


def constraint_violated(error: IntegrityError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def add_org(name: str = "Test Org") -> str:
    with session_scope() as session:
        org = Org(name=name)
        session.add(org)
    return org.id


def add_org_at(created_at: datetime) -> str:
    with session_scope() as session:
        org = Org(name="Timed Org", created_at=created_at)
        session.add(org)
    return org.id


def test_new_id_is_a_uuid4_string() -> None:
    value = new_id()

    assert isinstance(value, str)
    assert uuid.UUID(value).version == 4


def test_utc_now_is_timezone_aware_utc() -> None:
    assert utc_now().tzinfo is UTC


def test_naive_datetime_is_rejected() -> None:
    naive = datetime(2026, 11, 1, 9, 30)  # noqa: DTZ001 - a naive datetime is what's under test

    with pytest.raises(StatementError, match="naive"), session_scope() as session:
        session.add(Org(name="Naive Org", created_at=naive))
        session.flush()


def test_created_at_reads_back_as_utc_whatever_the_connection_time_zone() -> None:
    written = datetime(2026, 11, 1, 15, 0, tzinfo=IST)
    org_id = add_org_at(written)

    with session_scope(read_only=True) as session:
        # LOCAL: ends with the transaction, so the pooled connection doesn't keep it.
        session.execute(text("SET LOCAL TIME ZONE 'Asia/Kolkata'"))
        created_at = session.scalar(select(Org.created_at).where(Org.id == org_id))
    with session_scope(read_only=True) as session:
        time_zone_after = session.scalar(text("SHOW TIME ZONE"))

    assert time_zone_after != "Asia/Kolkata"
    assert created_at is not None
    assert created_at.tzinfo is UTC
    assert created_at == written


def test_created_at_defaults_to_now() -> None:
    before = utc_now()
    org_id = add_org()

    with session_scope(read_only=True) as session:
        created_at = session.scalar(select(Org.created_at).where(Org.id == org_id))

    assert created_at is not None
    assert before <= created_at <= utc_now()


@pytest.mark.parametrize("role", list(Role))
def test_every_role_is_accepted(role: Role) -> None:
    org_id = add_org()

    with session_scope() as session:
        session.add(User(org_id=org_id, email=f"{role}@example.com", name="Test", role=role))


def test_unknown_role_is_refused_by_the_database() -> None:
    org_id = add_org()

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(User(org_id=org_id, email="boss@example.com", name="Test", role="superuser"))

    assert constraint_violated(error.value) == "ck_users_role"


def test_email_with_capitals_is_refused_by_the_database() -> None:
    org_id = add_org()

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(User(org_id=org_id, email="Rep@Example.com", name="Test", role=Role.REP))

    assert constraint_violated(error.value) == "ck_users_email_lowercase"


def test_user_in_a_missing_org_is_refused() -> None:
    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(User(org_id=new_id(), email="rep@example.com", name="Test", role=Role.REP))

    assert constraint_violated(error.value) == "fk_users_org_id_orgs"


def test_shop_in_a_missing_org_is_refused() -> None:
    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(Shop(org_id=new_id(), name="Test Shop", area="Test Area"))

    assert constraint_violated(error.value) == "fk_shops_org_id_orgs"


def test_shop_may_be_recorded_unless_marked() -> None:
    org_id = add_org()

    with session_scope() as session:
        shop = Shop(org_id=org_id, name="Test Shop", area="Test Area")
        session.add(shop)

    assert shop.do_not_record is False


@pytest.mark.parametrize(
    ("make_duplicate", "constraint"),
    [
        (lambda org_id: Org(name="Test Org"), "uq_orgs_name"),
        (
            lambda org_id: User(org_id=org_id, email="dup@example.com", name="B", role=Role.REP),
            "uq_users_email",
        ),
        (
            lambda org_id: Shop(org_id=org_id, name="Dup Shop", area="Dup Area"),
            "uq_shops_org_id_name_area",
        ),
    ],
)
def test_natural_keys_are_unique(make_duplicate: Callable[[str], object], constraint: str) -> None:
    org_id = add_org("Test Org")
    with session_scope() as session:
        session.add(User(org_id=org_id, email="dup@example.com", name="A", role=Role.REP))
        session.add(Shop(org_id=org_id, name="Dup Shop", area="Dup Area"))

    with pytest.raises(IntegrityError) as error, session_scope() as session:
        session.add(make_duplicate(org_id))

    assert constraint_violated(error.value) == constraint
