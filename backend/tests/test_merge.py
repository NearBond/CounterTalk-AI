"""Merging issues (batch 15c): a person merges one of a team's issues into another of its
category. The routes move, the surviving issue takes the merged one's severity, due date and
seen times, each gets an IssueEvent, and a cause across teams stays one cause."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import select, update

from countertalk import inbox, issues
from countertalk.db import session_scope
from countertalk.errors import Conflict, Invalid, NotFound
from countertalk.models import Insight, InsightRoute, Issue, IssueEvent, Team, User, Visit
from countertalk.summaries import CategoryArea, check, org_numbers, team_numbers
from scripts import _k6
from tests.conftest import lock_waiters
from tests.test_extraction import insight
from tests.test_inbox_routing import only_visit_of
from tests.test_issue_changes import change, now_of
from tests.test_narrative import at_shop, day
from tests.test_review import PACK
from tests.test_roles import tiny_org
from tests.test_summaries import EXPECTED, visit_at
from tests.test_summaries import issue_of as k6_issue_of
from tests.test_summary_jobs import LAST_WEEK, team
from tests.test_visits import Org_
from tests.test_worker import wait_for

LOW = insight(category="payments", severity="low", suggested_team="money")
HIGH = insight(category="payments", severity="high", suggested_team="money")


def money_issue(visit_id: str) -> str:
    """The money team's issue its insight is in."""
    with session_scope(read_only=True) as session:
        found = session.scalars(select(Insight.id).where(Insight.visit_id == visit_id)).one()
    org_team = issues_by_team(found)
    return org_team["money"]


def issues_by_team(insight_id: str) -> dict[str, str]:
    """Each of the insight's routes' issues, by its team's key."""
    with session_scope(read_only=True) as session:
        query = (
            select(Team.key, InsightRoute.issue_id)
            .join(InsightRoute, InsightRoute.team_id == Team.id)
            .where(InsightRoute.insight_id == insight_id)
        )
        return {key: issue_id for key, issue_id in session.execute(query)}


def merge(org: Org_, into: str, merged: str, *, versions: tuple[int, int] | None = None) -> Issue:
    into_version, merged_version = versions or (now_of(into).version, now_of(merged).version)
    with session_scope() as session:
        return issues.merge(
            session,
            lambda: PACK,
            org.org_id,
            org.rep_id,
            (into, into_version),
            (merged, merged_version),
        )


def refused(org: Org_, into: str, merged: str, **kwargs: Any) -> str:
    with pytest.raises((Conflict, Invalid, NotFound)) as error:
        merge(org, into, merged, **kwargs)
    return error.value.code


def two_issues() -> tuple[Org_, str, str, str]:
    """The tiny org's money team with two payment issues, a low one here and a high one in
    another area: the org, both issues' IDs, and the high one's visit."""
    org = tiny_org()
    low = visit_at(org, day(0), day(0), LOW)
    far = at_shop(org, "Far Shop", area="Far Area")
    high = visit_at(far, day(2), day(2), HIGH)
    return org, money_issue(low), money_issue(high), high


@pytest.fixture
def two() -> tuple[Org_, str, str, str]:
    return two_issues()


def test_a_merge_moves_the_routes_and_the_surviving_issue_takes_their_severity(
    two: tuple[Org_, str, str, str],
) -> None:
    org, into, merged, high_visit = two
    before, merged_before = now_of(into), now_of(merged)

    merge(org, into, merged)

    after, gone = now_of(into), now_of(merged)
    assert money_issue(high_visit) == into  # the rep's page follows it
    assert (gone.status, gone.merged_into_id, gone.version) == (
        "merged",
        into,
        merged_before.version + 1,
    )
    assert (after.severity, after.due_at, after.version) == (
        "high",
        min(before.due_at, merged_before.due_at),
        before.version + 1,
    )
    assert (after.first_seen_at, after.last_seen_at) == (
        before.first_seen_at,
        merged_before.last_seen_at,
    )
    with session_scope(read_only=True) as session:
        events = session.execute(
            select(IssueEvent.issue_id, IssueEvent.field, IssueEvent.from_value)
            .add_columns(IssueEvent.to_value, IssueEvent.actor_id)
            .where(IssueEvent.issue_id.in_([into, merged]))
        ).all()
    assert sorted(map(tuple, events)) == sorted(
        [
            (merged, "status", "open", "merged", org.rep_id),
            (into, "merge", None, merged, org.rep_id),
        ]
    )


def test_merging_again_into_the_same_issue_changes_nothing(
    two: tuple[Org_, str, str, str],
) -> None:
    org, into, merged, _ = two
    versions = (now_of(into).version, now_of(merged).version)
    merge(org, into, merged, versions=versions)
    after = (now_of(into).version, now_of(merged).version)

    assert merge(org, into, merged, versions=versions).id == into

    assert (now_of(into).version, now_of(merged).version) == after


def test_a_merge_is_refused_for_a_moved_version_an_unlike_issue_or_a_closed_one(
    two: tuple[Org_, str, str, str],
) -> None:
    org, into, merged, _ = two
    field = issues_by_team(_only_insight(org, "field"))["field"]

    assert refused(org, into, merged, versions=(now_of(into).version, 0)) == "issue_changed"
    assert refused(org, into, into) == "merge_itself"
    assert refused(org, into, field) == "merge_unlike"  # another team's
    change(org, org.rep_id, merged, now_of(merged).version, status="resolved", note="Paid.")
    assert refused(org, into, merged) == "issue_closed"
    assert (now_of(into).status, now_of(merged).status) == ("open", "resolved")


def _only_insight(org: Org_, team_key: str) -> str:
    """The insight the high payment sent to this team too."""
    with session_scope(read_only=True) as session:
        return session.scalars(
            select(InsightRoute.insight_id).where(
                InsightRoute.team_id == team(org.org_id, team_key)
            )
        ).one()


def test_the_same_issue_spelled_twice_is_merging_it_into_itself(
    two: tuple[Org_, str, str, str],
) -> None:
    org, into, _, _ = two
    version = now_of(into).version

    assert refused(org, into, into.upper(), versions=(version, version)) == "merge_itself"


def test_an_issue_with_no_findings_left_can_t_be_merged(two: tuple[Org_, str, str, str]) -> None:
    org, into, merged, _ = two
    with session_scope() as session:  # its one route moved away, as a Move leaves it
        session.execute(
            update(InsightRoute).where(InsightRoute.issue_id == merged).values(issue_id=None)
        )

    assert refused(org, into, merged) == "issue_empty"


def test_merging_into_an_issue_merged_elsewhere_is_409(two: tuple[Org_, str, str, str]) -> None:
    org, into, merged, _ = two
    third = money_issue(
        visit_at(at_shop(org, "Third Shop", area="Third Area"), day(3), day(3), LOW)
    )
    merge(org, into, merged)

    assert refused(org, merged, third) == "issue_merged"


def test_a_merge_takes_its_insights_locks_before_any_issue_s(
    two: tuple[Org_, str, str, str],
) -> None:
    # A move locks the insight, then the issues: a merge takes them in that order too, so the
    # two can't deadlock.
    org, into, merged, high_visit = two
    with session_scope(read_only=True) as session:
        held = session.scalars(select(Insight.id).where(Insight.visit_id == high_visit)).one()

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        session.execute(select(Insight.id).where(Insight.id == held).with_for_update())
        merging = pool.submit(merge, org, into, merged)
        wait_for(lambda: lock_waiters() == 1, "the merge never waited for the insight")
        with session_scope() as other:  # the issues aren't locked yet
            locked = other.execute(
                select(Issue.id).where(Issue.id.in_([into, merged])).with_for_update(nowait=True)
            ).all()
        session.commit()

        merging.result(timeout=10)
    assert (len(locked), now_of(merged).status) == (2, "merged")


def test_merging_an_issue_merged_elsewhere_is_409(two: tuple[Org_, str, str, str]) -> None:
    org, into, merged, _ = two
    third = money_issue(
        visit_at(at_shop(org, "Third Shop", area="Third Area"), day(3), day(3), LOW)
    )
    merge(org, into, merged)

    assert refused(org, third, merged) == "issue_merged"


def test_a_merge_racing_a_status_change_loses_with_409(two: tuple[Org_, str, str, str]) -> None:
    org, into, merged, _ = two
    versions = (now_of(into).version, now_of(merged).version)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        issues.change(
            session, org.org_id, org.rep_id, merged, versions[1], status="in_progress"
        )  # holds the merged one, uncommitted
        merging = pool.submit(merge, org, into, merged, versions=versions)
        wait_for(lambda: lock_waiters() == 1, "the merge never waited for the change")
        session.commit()

        with pytest.raises(Conflict) as lost:
            merging.result(timeout=10)
    assert lost.value.code == "issue_changed"
    assert (now_of(merged).status, routes_in(merged)) == ("in_progress", 1)


def routes_in(issue_id: str) -> int:
    with session_scope(read_only=True) as session:
        return len(
            session.scalars(select(InsightRoute).where(InsightRoute.issue_id == issue_id)).all()
        )


def test_another_org_s_issue_is_404(two: tuple[Org_, str, str, str]) -> None:
    org, into, _, _ = two
    theirs = tiny_org("Their Org")
    theirs_issue = money_issue(visit_at(theirs, day(0), day(0), LOW))

    assert refused(org, into, theirs_issue) == "issue_not_found"


# --- a cause stays one cause -----------------------------------------------------------------


def test_a_two_team_insight_and_a_merge_stay_one_cause(two: tuple[Org_, str, str, str]) -> None:
    # The high payment went to money and field. Merged into the low one's money issue (another
    # key), grouped by key alone it would sit in two causes: money's, and field's own.
    org, into, merged, high_visit = two

    merge(org, into, merged)

    with session_scope(read_only=True) as session:
        found = org_numbers(session, PACK, org.org_id, LAST_WEEK)
        problems = check(session, org.org_id, found)
    [cause] = found.causes
    assert ([w.team_key for w in cause.teams], cause.measures.insights) == (["field", "money"], 2)
    assert (problems, found.totals.insights) == ([], 2)
    # One cause in each area, not money's and field's apart.
    assert found.by_category_area == [
        CategoryArea("payments", "Far Area", 1, 1),
        CategoryArea("payments", "Test Area", 1, 1),
    ]
    assert only_visit_of(_only_insight(org, "field")) == high_visit


# --- K6's P1 after the merge -----------------------------------------------------------------


def test_k6_s_p1_after_e_s_issue_is_merged_into_it() -> None:
    world = _k6.load()
    written = EXPECTED["written"]["P1"]
    finance = world.teams["finance"]
    with session_scope(read_only=True) as session:
        e_insight = session.scalars(
            select(Insight.id)
            .join(Visit, Visit.id == Insight.visit_id)
            .where(Insight.id.in_(world.insights["P1"]), Visit.shop_id == world.shops["E"])
        ).one()
    p1 = k6_issue_of(world, world.insights["P1"][0], "finance")
    e_issue = k6_issue_of(world, e_insight, "finance")
    assert e_issue != p1  # its scheme isn't linked: an issue of its own

    member = k6_member(world)
    with session_scope() as session:
        issues.merge(
            session,
            lambda: world.pack,
            world.org_id,
            member,
            (p1, now_of(p1).version),
            (e_issue, now_of(e_issue).version),
        )

    with session_scope(read_only=True) as session:
        [card] = [
            c
            for c in inbox.cards(
                session, lambda: world.pack, world.org_id, finance, inbox.NO_FILTERS
            )
            if c.issue.id == p1
        ]
        numbers = team_numbers(session, world.pack, world.org_id, finance, world.week(6))
    after = written["after_merge"]
    assert (card.shops, card.insights, card.reps, card.raised_again) == (
        after["shops"],
        after["insights"],
        after["reps"],
        after["raised_again"] == 1,
    )
    rank, top = next((n, i) for n, i in enumerate(numbers.issues, start=1) if i.issue_id == p1)
    assert (rank, top.measures.score, top.measures.label, top.measures.shops) == (
        written["W6_finance"]["rank"],
        written["W6_finance"]["score"],
        written["W6"],
        written["W6_shops"],
    )


def k6_member(world: _k6.World) -> str:
    with session_scope(read_only=True) as session:
        return session.scalars(
            select(User.id).where(User.org_id == world.org_id, User.email.startswith("finance-"))
        ).one()
