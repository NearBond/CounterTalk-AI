"""scripts/add_user.py and users.add_user (batch 16): made-up users of every role, to sign in as.
A team member has a team and no one else does; only example.com emails; the password comes from
NEW_PASSWORD and is never printed; an email already there changes nothing."""

import os
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from countertalk import auth, users
from countertalk.config import BACKEND_DIR
from countertalk.db import session_scope
from countertalk.errors import Invalid, NotFound
from countertalk.models import Role, User
from tests.test_roles import tiny_org
from tests.test_visits import Org_

PASSWORD = "a long made-up password"


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def add(org_id: str, email: str, role: str, team: str | None = None) -> User | None:
    with session_scope() as session:
        found = users.add_user(session, org_id, email, "Made Up", role, team, PASSWORD)
        if found is not None:
            session.expunge(found)
        return found


def count() -> int:
    with session_scope(read_only=True) as session:
        return session.scalar(select(func.count()).select_from(User)) or 0


@pytest.mark.parametrize(
    ("role", "team"),
    [(Role.REP, None), (Role.TEAM_MEMBER, "money"), (Role.MANAGER, None), (Role.ADMIN, None)],
)
def test_each_role_is_added_and_signs_in(org: Org_, role: str, team: str | None) -> None:
    found = add(org.org_id, f"New-{role}@Example.com", role, team)

    assert found is not None
    assert (found.email, found.role, found.team_id is not None) == (
        f"new-{role}@example.com",
        role,
        team is not None,
    )
    assert auth.password_matches(found.password_hash, PASSWORD)


@pytest.mark.parametrize(
    ("role", "team"),
    [(Role.TEAM_MEMBER, None), (Role.MANAGER, "money"), (Role.REP, "money")],
)
def test_a_team_is_a_team_member_s_and_only_theirs(org: Org_, role: str, team: str | None) -> None:
    before = count()

    with pytest.raises(Invalid) as refused:
        add(org.org_id, "someone@example.com", role, team)

    assert refused.value.code == "team_member_team"
    assert count() == before


@pytest.mark.parametrize(
    ("email", "role", "team", "code"),
    [
        ("someone@real-company.in", Role.REP, None, "example_email_only"),
        ("@example.com", Role.REP, None, "example_email_only"),
        ("someone@example.com", "superuser", None, "unknown_role"),
        ("someone@example.com", Role.TEAM_MEMBER, "no_such_team", "team_not_found"),
    ],
)
def test_anything_else_is_refused(
    org: Org_, email: str, role: str, team: str | None, code: str
) -> None:
    with pytest.raises((Invalid, NotFound)) as refused:
        add(org.org_id, email, role, team)

    assert refused.value.code == code


def test_an_unknown_org_is_404() -> None:
    with pytest.raises(NotFound) as refused:
        add("not-an-org", "someone@example.com", Role.REP)

    assert refused.value.code == "org_not_found"


def test_an_email_already_there_changes_nothing(org: Org_) -> None:
    first = add(org.org_id, "twice@example.com", Role.MANAGER)
    before = count()

    again = add(org.org_id, "Twice@example.com", Role.ADMIN)

    assert first is not None and again is None
    assert count() == before
    with session_scope(read_only=True) as session:
        assert (
            session.scalars(select(User.role).where(User.email == "twice@example.com")).one()
            == "manager"
        )


def run(*args: str, password: str | None = PASSWORD) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "NEW_PASSWORD"}
    if password is not None:
        env["NEW_PASSWORD"] = password
    return subprocess.run(
        [sys.executable, str(BACKEND_DIR / "scripts" / "add_user.py"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_script_adds_a_team_member_and_never_prints_the_password(org: Org_) -> None:
    added = run(
        org.org_id, "finance-two@example.com", "Finance Two", "team_member", "--team", "money"
    )
    again = run(
        org.org_id, "finance-two@example.com", "Finance Two", "team_member", "--team", "money"
    )

    assert (added.returncode, added.stderr) == (0, "")
    assert added.stdout == (
        "added finance-two@example.com, team_member in team money:"
        " sign in with the password given\n"
    )
    assert (again.returncode, again.stdout) == (
        0,
        "a user has that email already: nothing changed (finance-two@example.com)\n",
    )
    for done in (added, again):
        assert PASSWORD not in done.stdout + done.stderr


@pytest.mark.parametrize(
    ("args", "password", "said"),
    [
        (("manager",), None, "no password: set NEW_PASSWORD for this command\n"),
        (
            ("manager",),
            "Xq7-z",
            "user not added: password_too_short: A password needs 12 characters or more\n",
        ),
        (
            ("team_member",),
            PASSWORD,
            "user not added: team_member_team:"
            " A team member needs --team, and only a team member\n",
        ),
    ],
)
def test_the_script_refuses_in_one_line(
    org: Org_, args: tuple[str, ...], password: str | None, said: str
) -> None:
    done = run(org.org_id, "someone@example.com", "Someone", *args, password=password)

    assert (done.returncode, done.stdout, done.stderr) == (1, "", said)
    if password is not None:
        assert password not in done.stderr
