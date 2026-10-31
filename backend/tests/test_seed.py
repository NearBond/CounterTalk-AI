import threading

import pytest
from sqlalchemy import func, select

from countertalk.db import session_scope
from countertalk.errors import Conflict
from countertalk.limits import LOCK_TIMEOUT_MS
from countertalk.models import Org, Role, Shop, User
from scripts.seed_dev import DEMO_ORG_NAME, DEMO_REP, DEMO_SHOPS, main, seed

# A racing seed waits at most one lock timeout for the other; allow two before giving up.
RACE_TIMEOUT_S = 2 * LOCK_TIMEOUT_MS / 1000


def counts() -> tuple[int, int, int]:
    with session_scope(read_only=True) as session:
        return (
            session.scalar(select(func.count()).select_from(Org)) or 0,
            session.scalar(select(func.count()).select_from(User)) or 0,
            session.scalar(select(func.count()).select_from(Shop)) or 0,
        )


def test_seed_creates_the_demo_org_one_rep_and_its_shops() -> None:
    with session_scope() as session:
        seed(session)

    with session_scope(read_only=True) as session:
        org = session.scalars(select(Org)).one()
        rep = session.scalars(select(User)).one()
        shops = session.scalars(select(Shop)).all()
    assert org.name == DEMO_ORG_NAME
    assert rep.org_id == org.id
    assert rep.role == Role.REP
    assert rep.email.endswith("@example.com")
    assert 3 <= len(shops) <= 5
    assert {shop.org_id for shop in shops} == {org.id}


def test_seeding_twice_creates_no_duplicates() -> None:
    with session_scope() as session:
        first = seed(session)
    with session_scope() as session:
        second = seed(session)

    assert counts() == (1, 1, len(DEMO_SHOPS))
    assert (first.created, first.existing) == (sum(counts()), 0)
    assert (second.created, second.existing) == (0, sum(counts()))


def test_seeding_again_keeps_changes_made_since() -> None:
    with session_scope() as session:
        seed(session)
    with session_scope() as session:
        shop = session.scalars(select(Shop).limit(1)).one()
        shop.do_not_record = True
        changed_id = shop.id

    with session_scope() as session:
        seed(session)

    with session_scope(read_only=True) as session:
        shop = session.get(Shop, changed_id)
    assert shop is not None
    assert shop.do_not_record is True


def test_seed_refuses_when_the_demo_rep_belongs_to_another_org() -> None:
    with session_scope() as session:
        other = Org(name="Other Org")
        session.add(other)
        session.flush()
        session.add(User(org_id=other.id, email=DEMO_REP["email"], name="Other", role=Role.REP))

    with pytest.raises(Conflict, match="another org"), session_scope() as session:
        seed(session)

    assert counts() == (1, 1, 0)


def test_two_seeds_racing_create_no_duplicates() -> None:
    start = threading.Barrier(2, timeout=RACE_TIMEOUT_S)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            with session_scope() as session:
                start.wait()
                seed(session)
        except BaseException as error:  # noqa: BLE001 - collected and checked below
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=RACE_TIMEOUT_S)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert counts() == (1, 1, len(DEMO_SHOPS))


def test_main_prints_what_it_created(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    main()

    total = sum(counts())
    first, second = capsys.readouterr().out.strip().splitlines()
    assert first == f"seeded: created {total}, already there 0"
    assert second == f"seeded: created 0, already there {total}"
