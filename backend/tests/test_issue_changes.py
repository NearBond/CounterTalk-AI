"""issues.change: a team member's change to an issue, compare-and-set on its version (batch 15)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from countertalk import issues, limits, routing
from countertalk.db import session_scope
from countertalk.errors import Conflict, Forbidden, Invalid, NotFound
from countertalk.models import Issue, IssueEvent, Role, User, utc_now
from tests.conftest import lock_waiters
from tests.test_extraction import insight
from tests.test_issues import confirmed, issues_of, resolve
from tests.test_roles import tiny_org
from tests.test_visits import Org_, make_org
from tests.test_worker import wait_for


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def a_member(org: Org_, name: str = "Finance member", team: str = "money") -> str:
    """A member of one of the org's teams: their user ID."""
    with session_scope() as session:
        email = f"{name.split()[0].lower()}-{org.org_id[:8]}@example.com"
        team_id = routing.team_ids(session, org.org_id)[team]
        user = User(
            org_id=org.org_id, email=email, name=name, role=Role.TEAM_MEMBER, team_id=team_id
        )
        session.add(user)
        session.flush()
        return user.id


def an_issue(org: Org_) -> Issue:
    confirmed(org)
    [issue] = issues_of(org)
    return issue


def change(org: Org_, actor: str, issue_id: str, version: int, **changes: Any) -> Issue:
    with session_scope() as session:
        return issues.change(session, org.org_id, actor, issue_id, version, **changes)


def code_of(org: Org_, actor: str, issue_id: str, version: int, **changes: Any) -> str:
    with pytest.raises((Conflict, Forbidden, Invalid, NotFound)) as refused:
        change(org, actor, issue_id, version, **changes)
    return refused.value.code


def history(issue_id: str) -> list[tuple[str, str, str | None, str | None, str | None]]:
    with session_scope(read_only=True) as session:
        query = select(IssueEvent).where(IssueEvent.issue_id == issue_id)
        return [
            (e.actor_id, e.field, e.from_value, e.to_value, e.note)
            for e in session.scalars(query.order_by(IssueEvent.created_at, IssueEvent.field))
        ]


def now_of(issue_id: str) -> Issue:
    with session_scope(read_only=True) as session:
        return session.get_one(Issue, issue_id)


def test_assign_to_me_then_in_progress_then_resolved_with_a_note(org: Org_) -> None:
    member, issue = a_member(org), an_issue(org)

    mine = change(org, member, issue.id, 1, assign_to_me=True)
    started = change(org, member, issue.id, 2, status="in_progress")
    done = change(org, member, issue.id, 3, status="resolved", note="  Credit note sent.  ")

    assert (mine.owner_id, mine.version) == (member, 2)
    assert (started.status, started.version) == ("in_progress", 3)
    assert (done.status, done.resolution_note, done.version) == ("resolved", "Credit note sent.", 4)
    assert done.resolved_at is not None
    assert history(issue.id) == [
        (member, "owner", None, member, None),
        (member, "status", "open", "in_progress", None),
        (member, "status", "in_progress", "resolved", "Credit note sent."),
    ]


@pytest.mark.parametrize(
    ("note", "code"),
    [(None, "note_required"), ("   ", "note_required"), ("x" * 2_001, "note_too_long")],
)
def test_resolving_needs_a_note(org: Org_, note: str | None, code: str) -> None:
    member, issue = a_member(org), an_issue(org)

    assert code_of(org, member, issue.id, 1, status="resolved", note=note) == code
    assert (now_of(issue.id).status, now_of(issue.id).version) == ("open", 1)
    assert limits.RESOLUTION_NOTE_MAX_CHARS == 2_000


def test_a_change_made_on_an_old_read_is_refused_and_writes_nothing(org: Org_) -> None:
    member, issue = a_member(org), an_issue(org)
    change(org, member, issue.id, 1, status="in_progress")

    assert code_of(org, member, issue.id, 1, status="resolved", note="Done.") == "issue_changed"
    assert (now_of(issue.id).status, now_of(issue.id).version) == ("in_progress", 2)
    assert len(history(issue.id)) == 1


def test_two_team_members_changing_one_issue_the_second_is_told(org: Org_) -> None:
    first, second = a_member(org), a_member(org, "Second member")
    issue = an_issue(org)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        issues.change(session, org.org_id, first, issue.id, 1, status="in_progress")
        other = pool.submit(code_of, org, second, issue.id, 1, status="resolved", note="Sent it.")
        wait_for(lambda: lock_waiters() == 1, "the second change never waited for the first")
        session.commit()

        assert other.result(timeout=10) == "issue_changed"
    after = now_of(issue.id)
    assert (after.status, after.resolution_note, after.version) == ("in_progress", None, 2)


def test_a_resolved_issue_can_t_be_changed_by_hand(org: Org_) -> None:
    member, issue = a_member(org), an_issue(org)
    resolve(issue.id, days_ago=1)

    assert code_of(org, member, issue.id, 1, status="open") == "issue_closed"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"status": "merged"}, "status_invalid"),
        ({"status": "closed"}, "status_invalid"),
        ({"status": "in_progress", "note": "Why?"}, "note_only_when_resolving"),
        ({"note": "Just a note."}, "note_only_when_resolving"),
    ],
)
def test_a_change_that_isn_t_one_is_refused(org: Org_, changes: dict[str, Any], code: str) -> None:
    member, issue = a_member(org), an_issue(org)

    assert code_of(org, member, issue.id, 1, **changes) == code


def test_only_the_owner_changes_the_due_date(org: Org_) -> None:
    owner, other, issue = a_member(org), a_member(org, "Other member"), an_issue(org)
    later = utc_now() + timedelta(days=5)

    assert code_of(org, owner, issue.id, 1, due_at=later) == "not_owner"
    mine = change(org, owner, issue.id, 1, assign_to_me=True)
    assert code_of(org, other, issue.id, 2, due_at=later) == "not_owner"
    assert code_of(org, owner, issue.id, 2, due_at=utc_now() - timedelta(hours=1)) == (
        "due_in_past"
    )
    moved = change(org, owner, issue.id, 2, due_at=later)

    assert (moved.due_at, moved.version) == (later, 3)
    assert history(issue.id)[-1] == (
        owner,
        "due_at",
        mine.due_at.isoformat(),
        later.isoformat(),
        None,
    )


def test_assign_to_me_and_a_due_date_in_one_change_is_one_version(org: Org_) -> None:
    owner, issue = a_member(org), an_issue(org)
    later: datetime = utc_now() + timedelta(days=3)

    both = change(org, owner, issue.id, 1, assign_to_me=True, due_at=later)

    assert (both.owner_id, both.due_at, both.version) == (owner, later, 2)
    assert sorted(field for _, field, _, _, _ in history(issue.id)) == ["due_at", "owner"]


def test_a_change_to_what_it_is_already_writes_nothing(org: Org_) -> None:
    member, issue = a_member(org), an_issue(org)
    change(org, member, issue.id, 1, assign_to_me=True)

    same = change(org, member, issue.id, 2, assign_to_me=True, status="open")

    assert same.version == 2
    assert len(history(issue.id)) == 1


def test_another_org_s_issue_is_not_found(org: Org_) -> None:
    issue = an_issue(org)
    other = make_org("Another Org")

    assert code_of(other, other.rep_id, issue.id, 1, status="in_progress") == "issue_not_found"
    assert code_of(org, org.rep_id, "not-an-id", 1, status="in_progress") == "issue_not_found"


def test_a_merged_issue_says_so(org: Org_) -> None:
    member, issue = a_member(org), an_issue(org)
    confirmed(org, insight(category="supply"))  # another issue to merge into
    other = next(each for each in issues_of(org) if each.id != issue.id)
    with session_scope() as session:
        session.execute(
            update(Issue)
            .where(Issue.id == issue.id)
            .values(status="merged", merged_into_id=other.id)
        )

    assert code_of(org, member, issue.id, 1, status="in_progress") == "issue_merged"


def test_a_due_date_needs_its_time_zone(org: Org_) -> None:
    owner, issue = a_member(org), an_issue(org)
    change(org, owner, issue.id, 1, assign_to_me=True)
    naive = (utc_now() + timedelta(days=2)).replace(tzinfo=None)

    assert code_of(org, owner, issue.id, 2, due_at=naive) == "due_at_without_zone"


def test_a_refused_change_writes_nothing_even_if_its_caller_commits(org: Org_) -> None:
    member, issue = a_member(org), an_issue(org)
    past = utc_now() - timedelta(days=1)

    with session_scope() as session:  # commits as it closes
        with pytest.raises(Invalid):
            issues.change(session, org.org_id, member, issue.id, 1, assign_to_me=True, due_at=past)

    after = now_of(issue.id)
    assert (after.owner_id, after.version) == (None, 1)
    assert history(issue.id) == []
