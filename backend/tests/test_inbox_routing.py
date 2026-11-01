"""A team member's routing after confirm (batch 15): accept, Move, Also send and triage, each
compare-and-set on what they read, and each filing the route in its team's issue."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from countertalk import issues, routing
from countertalk.db import session_scope
from countertalk.errors import Conflict, NotFound
from countertalk.models import Insight, InsightRoute, Issue, RoutingDecision, Team, utc_now
from countertalk.routing import Seen
from tests.conftest import lock_waiters
from tests.test_extraction import insight
from tests.test_issues import confirm, confirmed, issues_of, memberships, resolve, visited_at
from tests.test_review import PACK, reviewed
from tests.test_roles import tiny_org
from tests.test_routing import a_third_team, routes_of, team_ids
from tests.test_visits import Org_
from tests.test_worker import wait_for

SUGGESTED = insight(category="demand", suggested_team="field")  # no table team: the AI's
UNPLACED = insight(category="demand", suggested_team=None)  # no team at all: triage
FIRST = Seen(1, 1)


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def only_insight(visit_id: str) -> str:
    with session_scope(read_only=True) as session:
        return session.scalars(select(Insight.id).where(Insight.visit_id == visit_id)).one()


def act(org: Org_, how: str, insight_id: str, *args: Any) -> InsightRoute:
    """One of routing's after-confirm changes, by the org's user, committed."""
    with session_scope() as session:
        if how == "accept":
            return routing.accept(session, org.org_id, insight_id, org.rep_id, *args)
        if how == "triage":
            return routing.assign_from_triage(
                session, PACK, org.org_id, insight_id, *args, org.rep_id
            )
        change = routing.move if how == "move" else routing.also_send
        team, seen = args
        return change(session, PACK, org.org_id, insight_id, team, org.rep_id, seen)


def code_of(org: Org_, how: str, insight_id: str, *args: Any) -> str:
    with pytest.raises((Conflict, NotFound)) as refused:
        act(org, how, insight_id, *args)
    return refused.value.code


def decisions(insight_id: str) -> list[tuple[str, int | None, str | None, str | None]]:
    """Each history row a person decided: its action, slot, team and who."""
    with session_scope(read_only=True) as session:
        query = (
            select(RoutingDecision.action, RoutingDecision.slot, Team.key)
            .add_columns(RoutingDecision.decided_by)
            .outerjoin(Team, Team.id == RoutingDecision.team_id)
            .where(
                RoutingDecision.insight_id == insight_id,
                RoutingDecision.decided_by.is_not(None),
            )
            .order_by(RoutingDecision.created_at)
        )
        return [tuple(row) for row in session.execute(query)]


def issue_of(insight_id: str, slot: int = 1) -> Issue | None:
    with session_scope(read_only=True) as session:
        route = select(InsightRoute.issue_id).where(
            InsightRoute.insight_id == insight_id, InsightRoute.slot == slot
        )
        issue_id = session.scalar(route)
        return session.get(Issue, issue_id) if issue_id else None


def shown_issues(org: Org_) -> list[str]:
    """The org's issues that have routes, the inbox's own test (issues.with_routes)."""
    with session_scope(read_only=True) as session:
        query = select(Issue.id).where(Issue.org_id == org.org_id, issues.with_routes())
        return list(session.scalars(query.order_by(Issue.seq)))


# --- accept ----------------------------------------------------------------------------------


def test_accepting_the_ai_s_suggestion_makes_it_a_person_s(org: Org_) -> None:
    insight_id = only_insight(confirmed(org, SUGGESTED))
    assert routes_of(insight_id) == [(1, "field", "ai")]

    accepted = act(org, "accept", insight_id, FIRST)

    assert (accepted.routed_by, accepted.version) == ("person", 2)
    assert routes_of(insight_id) == [(1, "field", "person")]
    assert decisions(insight_id) == [("accept", 1, "field", org.rep_id)]


def test_only_a_suggestion_is_accepted_and_only_as_it_was_read(org: Org_) -> None:
    table_routed = only_insight(confirmed(org))  # payments: the table's money
    suggested = only_insight(confirmed(org, SUGGESTED))
    act(org, "accept", suggested, FIRST)

    assert code_of(org, "accept", table_routed, FIRST) == "nothing_to_accept"
    assert code_of(org, "accept", suggested, FIRST) == "route_changed"


# --- Move ------------------------------------------------------------------------------------


def test_a_move_files_the_route_in_the_new_team_s_issue_and_hides_the_emptied_one(
    org: Org_,
) -> None:
    insight_id = only_insight(confirmed(org, SUGGESTED))
    before = issue_of(insight_id)
    assert before is not None
    money = team_ids(org.org_id)["money"]

    moved = act(org, "move", insight_id, money, FIRST)

    after = issue_of(insight_id)
    assert after is not None
    assert (moved.team_id, moved.routed_by, moved.version) == (money, "person", 2)
    assert (after.team_id, after.issue_key) == (money, before.issue_key)
    assert decisions(insight_id) == [("move", 1, "money", org.rep_id)]
    # The field issue stays, with no routes, so the inbox hides it.
    assert {issue.id for issue in issues_of(org)} == {before.id, after.id}
    assert shown_issues(org) == [after.id]


def test_a_move_refuses_a_team_it_has_another_org_s_and_an_unconfirmed_insight(
    org: Org_,
) -> None:
    sent_twice = only_insight(confirmed(org, insight(category="payments", severity="high")))
    _, [pending] = reviewed(org)
    teams = team_ids(org.org_id)

    assert code_of(org, "move", sent_twice, teams["field"], FIRST) == "already_routed"
    assert code_of(org, "move", sent_twice, "not-a-team", FIRST) == "team_not_found"
    assert code_of(org, "move", pending, teams["field"], FIRST) == "insight_not_confirmed"


def test_two_moves_at_once_one_wins_and_the_insight_ends_in_one_team(org: Org_) -> None:
    insight_id = only_insight(confirmed(org, SUGGESTED))
    money, third = team_ids(org.org_id)["money"], a_third_team(org)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        routing.move(session, PACK, org.org_id, insight_id, money, org.rep_id, FIRST)
        second = pool.submit(code_of, org, "move", insight_id, third, FIRST)
        wait_for(lambda: lock_waiters() == 1, "the second move never waited for the first")
        session.commit()

        assert second.result(timeout=10) == "route_changed"
    assert routes_of(insight_id) == [(1, "money", "person")]


# --- Also send -------------------------------------------------------------------------------


def test_also_send_files_slot_2_in_that_team_s_issue_and_stops_at_two(org: Org_) -> None:
    insight_id = only_insight(confirmed(org))  # money
    field = team_ids(org.org_id)["field"]

    sent = act(org, "also_send", insight_id, field, FIRST)

    second_issue = issue_of(insight_id, slot=2)
    assert second_issue is not None
    assert (sent.slot, sent.routed_by, second_issue.team_id) == (2, "person", field)
    assert memberships(only_visit_of(insight_id))[1] == ("field", second_issue.id)
    assert code_of(org, "also_send", insight_id, a_third_team(org), FIRST) == "team_limit"
    assert code_of(org, "also_send", insight_id, field, Seen(1, 9)) == "route_changed"


def only_visit_of(insight_id: str) -> str:
    with session_scope(read_only=True) as session:
        return session.get_one(Insight, insight_id).visit_id


# --- triage ----------------------------------------------------------------------------------


def test_a_triage_assignment_files_it_in_the_team_s_issue(org: Org_) -> None:
    visit_id = confirmed(org, UNPLACED)
    insight_id = only_insight(visit_id)
    assert routes_of(insight_id) == []
    money = team_ids(org.org_id)["money"]

    assigned = act(org, "triage", insight_id, money)

    [issue] = issues_of(org)
    assert (assigned.slot, assigned.routed_by) == (1, "person")
    assert (issue.team_id, issue.category) == (money, "demand")
    assert memberships(visit_id) == [("money", issue.id)]
    assert decisions(insight_id) == [("triage_assign", 1, "money", org.rep_id)]


def test_a_second_insight_with_the_same_key_joins_the_issue_triage_made(org: Org_) -> None:
    first = only_insight(confirmed(org, UNPLACED))
    second = only_insight(confirmed(org, UNPLACED))
    money = team_ids(org.org_id)["money"]

    act(org, "triage", first, money)
    act(org, "triage", second, money)

    [issue] = issues_of(org)
    assert (
        memberships(only_visit_of(first))
        == memberships(only_visit_of(second))
        == [("money", issue.id)]
    )
    assert issue.version == 2


def test_two_triage_assignments_at_once_one_wins(org: Org_) -> None:
    insight_id = only_insight(confirmed(org, UNPLACED))
    teams = team_ids(org.org_id)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        routing.assign_from_triage(
            session, PACK, org.org_id, insight_id, teams["money"], org.rep_id
        )
        second = pool.submit(code_of, org, "triage", insight_id, teams["field"])
        wait_for(lambda: lock_waiters() == 1, "the second never waited for the first")
        session.commit()

        assert second.result(timeout=10) == "already_routed"
    assert routes_of(insight_id) == [(1, "money", "person")]


def test_an_unconfirmed_insight_isn_t_assigned_from_triage(org: Org_) -> None:
    visit_id, [insight_id] = reviewed(org, UNPLACED)

    assert code_of(org, "triage", insight_id, team_ids(org.org_id)["money"]) == (
        "insight_not_confirmed"
    )
    confirm(org, visit_id)
    assert issues_of(org) == []


# --- the issue a move leaves -----------------------------------------------------------------


def test_the_issue_a_move_leaves_is_worked_out_again_from_what_it_has_left(
    org: Org_, caplog: pytest.LogCaptureFixture
) -> None:
    high = only_insight(confirmed(org, insight(category="payments", severity="high")))
    low = only_insight(confirmed(org))  # payments, low: money's issue too
    left = issue_of(high)
    assert left is not None and left.severity == "high"
    third = a_third_team(org)
    caplog.clear()

    with caplog.at_level(logging.INFO, logger=issues.__name__):
        act(org, "move", high, third, FIRST)

    after = issue_of(low)
    assert after is not None and after.id == left.id
    assert (after.severity, after.version) == ("low", left.version + 1)
    assert after.first_seen_at == after.last_seen_at  # the low one's visit alone
    logged = [r.getMessage() for r in caplog.records if r.name == issues.__name__]
    assert logged == [
        f"issues attached visit={only_visit_of(high)} insight={high}"
        " created=1 reopened=0 joined=0 left=1"
    ]


def test_an_emptied_issue_joined_again_starts_fresh(org: Org_) -> None:
    high = only_insight(confirmed(org, insight(category="payments", severity="high")))
    emptied = issue_of(high)
    assert emptied is not None
    act(org, "move", high, a_third_team(org), FIRST)  # money's issue: no routes left
    before = utc_now()

    low = only_insight(confirmed(org))  # payments, low: money, the same key

    again = issue_of(low)
    assert again is not None and again.id == emptied.id
    assert again.severity == "low"
    assert before + timedelta(days=14) <= again.due_at <= utc_now() + timedelta(days=14)
    assert again.first_seen_at == again.last_seen_at


def held(issue_id: str) -> bool:
    """Whether another transaction holds the issue's row lock: NOWAIT, so never a wait."""
    try:
        with session_scope() as session:
            query = select(Issue.id).where(Issue.id == issue_id)
            session.execute(query.with_for_update(nowait=True))
    except DBAPIError as error:
        assert error.orig.sqlstate == "55P03"  # type: ignore[union-attr]  # lock_not_available
        return True
    return False


@pytest.mark.parametrize(("source", "target"), [("field", "money"), ("money", "field")])
def test_a_move_locks_the_issue_it_leaves_and_the_one_it_joins_in_team_then_key_order(
    org: Org_, source: str, target: str
) -> None:
    """§2.3: whichever comes first in (team_id, issue_key) order is locked first, so two moves
    crossing between the same two issues can't deadlock. The one taken second is held here:
    the move must already hold the first while it waits."""
    teams = team_ids(org.org_id)
    joined_one = only_insight(confirmed(org, UNPLACED))
    act(org, "triage", joined_one, teams[target])
    mover = only_insight(confirmed(org, UNPLACED))
    act(org, "triage", mover, teams[source])
    old, new = issue_of(mover), issue_of(joined_one)
    assert old is not None and new is not None
    first, second = (
        (old, new) if (old.team_id, old.issue_key) < (new.team_id, new.issue_key) else (new, old)
    )

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as holder:
        holder.execute(select(Issue.id).where(Issue.id == second.id).with_for_update())
        moving = pool.submit(act, org, "move", mover, teams[target], FIRST)
        wait_for(lambda: lock_waiters() == 1, "the move never waited for the issue held")

        assert held(first.id)
        holder.commit()
        moving.result(timeout=10)
    assert memberships(only_visit_of(mover)) == [(target, new.id)]


def test_an_insight_seen_before_a_resolve_starts_a_new_issue_instead_of_reopening_it(
    org: Org_,
) -> None:
    confirmed(org)
    [resolved] = issues_of(org)
    earlier, _ = reviewed(org)
    resolve(resolved.id, days_ago=0)
    with session_scope() as session:  # resolved an hour after that visit
        after_it = visited_at(earlier) + timedelta(hours=1)
        session.execute(update(Issue).where(Issue.id == resolved.id).values(resolved_at=after_it))

    confirm(org, earlier)

    [still, new] = issues_of(org)
    assert (still.id, still.status, still.resolution_note) == (
        resolved.id,
        "resolved",
        "Credit note sent.",
    )
    assert memberships(earlier) == [("money", new.id)]
