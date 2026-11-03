import threading

import pytest
from sqlalchemy import func, select

from countertalk.db import session_scope
from countertalk.errors import Conflict
from countertalk.limits import LOCK_TIMEOUT_MS
from countertalk.models import Distributor, Org, Role, Shop, User
from countertalk.playbooks import load_org_pack
from scripts.seed_dev import (
    DEMO_ORG_NAME,
    DEMO_PACK,
    DEMO_REP,
    DEMO_SHOPS,
    MAHARASHTRA,
    main,
    seed,
)

PACK = load_org_pack(*DEMO_PACK)
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
        seed(session, PACK)

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
        first = seed(session, PACK)
    with session_scope() as session:
        second = seed(session, PACK)

    assert counts() == (1, 1, len(DEMO_SHOPS))
    assert (first.created, first.existing) == (sum(counts()), 0)
    assert (second.created, second.existing) == (0, sum(counts()))


def test_seeding_again_keeps_changes_made_since() -> None:
    with session_scope() as session:
        seed(session, PACK)
    with session_scope() as session:
        shop = session.scalars(select(Shop).limit(1)).one()
        shop.do_not_record = True
        shop.beat = "Saturday route"
        changed_id = shop.id

    with session_scope() as session:
        seed(session, PACK)

    with session_scope(read_only=True) as session:
        shop = session.get(Shop, changed_id)
    assert shop is not None
    assert (shop.do_not_record, shop.beat) == (True, "Saturday route")


def test_the_demo_org_is_on_the_example_pack_with_its_shops_on_distributors() -> None:
    with session_scope() as session:
        seed(session, PACK)

    with session_scope(read_only=True) as session:
        org = session.scalars(select(Org)).one()
        shops = session.execute(
            select(Shop.name, Distributor.key, Shop.beat)
            .join(Distributor, Shop.distributor_id == Distributor.id)
            .order_by(Shop.name)
        ).all()
    assert (org.org_pack_id, org.org_pack_version) == DEMO_PACK
    assert [tuple(row) for row in shops] == sorted((n, d, b) for n, _, d, b in DEMO_SHOPS)


def test_seed_refuses_when_the_demo_rep_belongs_to_another_org() -> None:
    with session_scope() as session:
        other = Org(name="Other Org")
        session.add(other)
        session.flush()
        session.add(User(org_id=other.id, email=DEMO_REP["email"], name="Other", role=Role.REP))

    with pytest.raises(Conflict, match="another org"), session_scope() as session:
        seed(session, PACK)

    assert counts() == (1, 1, 0)


def test_two_seeds_racing_create_no_duplicates() -> None:
    start = threading.Barrier(2, timeout=RACE_TIMEOUT_S)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            with session_scope() as session:
                start.wait()
                seed(session, PACK)
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
    with session_scope(read_only=True) as session:
        org_id = session.scalars(select(Org.id)).one()
    assert first == f"seeded {DEMO_ORG_NAME} ({org_id}): created {total}, already there 0"
    assert second == f"seeded {DEMO_ORG_NAME} ({org_id}): created 0, already there {total}"


def test_a_shop_with_a_beat_but_no_distributor_gets_one_and_keeps_its_beat() -> None:
    with session_scope() as session:
        seed(session, PACK)
    with session_scope() as session:
        shop = session.scalars(select(Shop).where(Shop.name == "Sunrise Kirana")).one()
        shop.distributor_id, shop.beat = None, "Saturday route"

    with session_scope() as session:
        seed(session, PACK)

    with session_scope(read_only=True) as session:
        found = session.execute(
            select(Distributor.key, Shop.beat)
            .join(Distributor, Shop.distributor_id == Distributor.id)
            .where(Shop.name == "Sunrise Kirana")
        ).one()
    assert tuple(found) == ("d1", "Saturday route")


# --- the Maharashtra dev org (batch T3) -------------------------------------------------------


def test_the_maharashtra_org_has_its_pack_two_shops_and_no_rep() -> None:
    pack = load_org_pack(*MAHARASHTRA.pack)
    with session_scope() as session:
        first = seed(session, pack, MAHARASHTRA)
    with session_scope() as session:
        again = seed(session, pack, MAHARASHTRA)

    with session_scope(read_only=True) as session:
        org = session.scalars(select(Org)).one()
        shops = session.execute(
            select(Shop.name, Shop.area, Distributor.key, Shop.beat)
            .join(Distributor, Shop.distributor_id == Distributor.id)
            .order_by(Shop.name)
        ).all()
    assert (org.name, org.org_pack_id, org.org_pack_version) == (
        "Demo Org Maharashtra",
        "pepsico-maharashtra-demo",
        1,
    )
    assert [tuple(row) for row in shops] == [
        ("Grapevine General Store", "Nashik", "d3", "Nashik Wednesday"),
        ("Morning Star Kirana", "Pune", "d1", "Pune Monday"),
    ]
    assert counts() == (1, 0, 2)  # its reps come from add_user.py
    assert ((first.created, first.existing), (again.created, again.existing)) == ((3, 0), (0, 3))


def test_main_seeds_the_org_it_is_asked_for(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--org", "maharashtra"]) == 0

    with session_scope(read_only=True) as session:
        [(name, org_id)] = session.execute(select(Org.name, Org.id)).all()
    assert name == "Demo Org Maharashtra"
    assert capsys.readouterr().out == f"seeded {name} ({org_id}): created 3, already there 0\n"
