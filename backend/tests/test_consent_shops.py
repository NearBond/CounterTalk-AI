"""scripts/consent_shops.py: a paper yes for every shop in the data that hasn't answered (batch 17;
the user, 3 Nov), marked by one of its org's own people."""

import pytest
from sqlalchemy import select

from countertalk import consent
from countertalk.db import session_scope
from countertalk.models import Consent, Org, Role, Shop
from scripts.consent_shops import main
from tests.conftest import a_user
from tests.test_consent import unanswered_shop
from tests.test_visits import make_org


def answers(org_id: str) -> dict[str, str]:
    with session_scope(read_only=True) as session:
        shops = session.scalars(select(Shop.id).where(Shop.org_id == org_id)).all()
        return consent.current(session, org_id, shops)


def bare_org(name: str) -> str:
    """An org with one shop and no one in it who could mark a form."""
    with session_scope() as session:
        org = Org(name=name)
        session.add(org)
        session.flush()
        session.add(Shop(org_id=org.id, name="Lone Shop", area="Lone Area"))
        return org.id


def test_without_yes_it_counts_and_changes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    org = make_org()
    unanswered_shop(org)

    assert main([]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"Visit Test Org ({org.org_id}): to mark 1, by rep {org.rep_id}; already answered 1",
        "Nothing was changed. Run again with --yes to mark them.",
    ]
    assert len(answers(org.org_id)) == 1


def test_yes_marks_every_org_s_unanswered_shops_and_again_marks_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    org = make_org()
    shop_id = unanswered_shop(org)

    assert main(["--yes"]) == 0
    assert main(["--yes"]) == 0

    first, second = capsys.readouterr().out.splitlines()
    assert (
        first == f"Visit Test Org ({org.org_id}): marked 1, by rep {org.rep_id}; already answered 1"
    )
    assert second == f"Visit Test Org ({org.org_id}): marked 0; already answered 2"
    with session_scope(read_only=True) as session:
        [row] = session.scalars(select(Consent).where(Consent.shop_id == shop_id)).all()
    assert (row.decision, row.method, row.recorded_by) == ("yes", "paper", org.rep_id)


def test_an_org_with_no_rep_is_marked_by_its_manager(capsys: pytest.CaptureFixture[str]) -> None:
    org_id = bare_org("Managed Org")
    manager = a_user(org_id, Role.MANAGER, "Only Manager")
    a_user(org_id, Role.ADMIN, "Only Admin")

    assert main(["--yes"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"Managed Org ({org_id}): marked 1, by manager {manager}; already answered 0"
    ]


def test_an_org_with_no_one_to_mark_them_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    org_id = bare_org("Empty Org")

    assert main(["--yes"]) == 1

    out, err = capsys.readouterr()
    assert out.splitlines() == [
        f"Empty Org ({org_id}): 1 without an answer, and no one to mark them"
    ]
    assert err == "1 shops are still without an answer\n"
    assert answers(org_id) == {}
