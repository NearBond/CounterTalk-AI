"""issues.py: confirmed insights grouped into each team's issues (batch 15), on confirm."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from countertalk import extraction, issues, review
from countertalk.db import constraint_name, session_scope
from countertalk.errors import Conflict
from countertalk.models import (
    Distributor,
    Insight,
    InsightRoute,
    Issue,
    IssueEvent,
    IssueStatus,
    Shop,
    Team,
    Visit,
    new_id,
    utc_now,
)
from countertalk.playbooks import PLAYBOOKS_DIR
from tests.conftest import ORG_PACK_FIXTURES, lock_waiters
from tests.test_extraction import insight
from tests.test_quotes import PARAPHRASE
from tests.test_review import PACK, reviewed
from tests.test_roles import tiny_org
from tests.test_visits import Org_
from tests.test_worker import wait_for

HIGH_PAYMENT = insight(category="payments", severity="high", suggested_team="money")


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def confirm(org: Org_, visit_id: str) -> None:
    with session_scope() as session:
        review.confirm(session, lambda: PACK, org.org_id, org.rep_id, visit_id)


def confirmed(org: Org_, *found: dict[str, Any]) -> str:
    visit_id, _ = reviewed(org, *found)
    confirm(org, visit_id)
    return visit_id


def issues_of(org: Org_) -> list[Issue]:
    with session_scope(read_only=True) as session:
        query = select(Issue).where(Issue.org_id == org.org_id).order_by(Issue.seq)
        return list(session.scalars(query))


def memberships(visit_id: str) -> list[tuple[str, str | None]]:
    """Each route of the visit's insights: its team's key and its issue."""
    with session_scope(read_only=True) as session:
        query = (
            select(Team.key, InsightRoute.issue_id)
            .join(InsightRoute, InsightRoute.team_id == Team.id)
            .join(Insight, Insight.id == InsightRoute.insight_id)
            .where(Insight.visit_id == visit_id)
            .order_by(Insight.seq, InsightRoute.slot)
        )
        return [tuple(row) for row in session.execute(query)]


def distributor_of(org: Org_, key: str = "t1") -> str:
    """Gives the org's shop that distributor: its ID."""
    with session_scope() as session:
        query = select(Distributor.id).where(Distributor.org_id == org.org_id)
        found = session.scalars(query.where(Distributor.key == key)).one()
        session.execute(update(Shop).where(Shop.id == org.shop_id).values(distributor_id=found))
        return found


def visited_at(visit_id: str) -> datetime:
    with session_scope(read_only=True) as session:
        return session.get_one(Visit, visit_id).created_at


def resolve(issue_id: str, days_ago: float) -> None:
    with session_scope() as session:
        session.execute(
            update(Issue)
            .where(Issue.id == issue_id)
            .values(
                status=IssueStatus.RESOLVED,
                resolved_at=utc_now() - timedelta(days=days_ago),
                resolution_note="Credit note sent.",
            )
        )


# --- the key and the title -------------------------------------------------------------------


def shop(distributor_id: str | None = None, area: str = "Test Area") -> Shop:
    return Shop(name="A shop", area=area, distributor_id=distributor_id)


def test_the_key_is_the_category_then_each_group_by_value_in_order() -> None:
    found = Insight(category="supply", sku_id="sku-1", competitor_id=None, scheme_key=None)

    with_distributor = issues.key_fields(["sku", "distributor"], found, shop("dist-1"))
    no_sku = issues.key_fields(["sku", "distributor"], Insight(sku_id=None), shop("dist-1"))

    assert with_distributor == {"sku": "sku-1", "distributor": "dist-1"}
    assert issues.issue_key("supply", with_distributor) == "supply|sku-1|dist-1"
    assert issues.issue_key("supply", no_sku) == "supply|-|dist-1"


def test_a_shop_with_no_distributor_groups_by_its_area_in_its_place() -> None:
    fields = issues.key_fields(["distributor", "area"], Insight(), shop(None, "North"))

    assert fields == {"distributor": "area:North", "area": "North"}
    assert issues.issue_key("visibility", fields) == "visibility|area:North|North"


@pytest.mark.parametrize(
    ("area", "part"), [("A|B", "A%7CB"), ("50%", "50%25"), ("-", "%2D"), ("पूर्व", "पूर्व")]
)
def test_a_value_can_t_make_two_keys_look_alike(area: str, part: str) -> None:
    assert issues.issue_key("other", {"area": area}) == f"other|{part}"


def test_the_title_is_the_category_and_each_value_s_label() -> None:
    names = {"sku-1": "Crunchy Salted", "dist-1": "North Traders (example)"}

    supply = issues.title_of(PACK, "supply", {"sku": "sku-1", "distributor": "dist-1"}, names)
    no_sku = issues.title_of(PACK, "supply", {"sku": None, "distributor": "area:South"}, names)
    scheme = issues.title_of(
        PACK, "schemes", {"scheme": "crunchy_winter", "distributor": "dist-1"}, names
    )

    assert supply == "Supply and delivery · Crunchy Salted · North Traders (example)"
    assert no_sku == "Supply and delivery · South (no distributor)"
    assert scheme == "Schemes and promotions · Crunchy winter payout · North Traders (example)"


def test_issues_are_locked_team_first_then_key() -> None:
    """§2.3's order: two teams can share a key, so ordering by the key alone could deadlock."""

    def member(team: str, category: str) -> issues.Member:
        route = InsightRoute(team_id=team)
        return issues.Member(
            route, Insight(category=category, severity="low"), shop("d"), utc_now()
        )

    members = [
        member("team-b", "payments"),
        member("team-a", "supply"),
        member("team-a", "payments"),
    ]

    assert [(team, key) for team, key, _, _ in issues.plan(PACK, members)] == [
        ("team-a", "payments|d"),
        ("team-a", "supply|-|d"),
        ("team-b", "payments|d"),
    ]


# --- attaching on confirm --------------------------------------------------------------------


def test_confirm_files_each_route_in_its_team_s_issue(org: Org_) -> None:
    distributor = distributor_of(org)
    before = utc_now()

    visit_id = confirmed(org)

    [issue] = issues_of(org)
    with session_scope(read_only=True) as session:
        money = session.scalars(select(Team.id).where(Team.key == "money")).one()
    assert (issue.team_id, issue.category, issue.issue_key) == (
        money,
        "payments",
        f"payments|{distributor}",
    )
    assert issue.key_fields == {"distributor": distributor}
    assert issue.title_en == "Credit and payments · North Traders (example)"
    assert (issue.severity, issue.status, issue.version, issue.reopened_count) == (
        "low",
        "open",
        1,
        0,
    )
    # low: 14 days from the playbook.
    assert before + timedelta(days=14) <= issue.due_at <= utc_now() + timedelta(days=14)
    assert issue.first_seen_at == issue.last_seen_at == visited_at(visit_id)
    assert memberships(visit_id) == [("money", issue.id)]


def test_an_insight_sent_to_two_teams_is_in_one_issue_of_each(org: Org_) -> None:
    visit_id = confirmed(org, HIGH_PAYMENT)

    found = issues_of(org)
    assert len(found) == 2
    assert len({issue.issue_key for issue in found}) == 1
    by_id = {issue.id: issue for issue in found}
    teams = [(team, by_id[issue_id].team_id) for team, issue_id in memberships(visit_id)]
    assert [team for team, _ in teams] == ["money", "field"]
    assert len({team_id for _, team_id in teams}) == 2


def test_the_same_cause_again_joins_the_issue_and_a_higher_severity_brings_it_forward(
    org: Org_,
) -> None:
    first = confirmed(org)
    [issue] = issues_of(org)

    second = confirmed(org, HIGH_PAYMENT)

    found = issues_of(org)
    joined = next(each for each in found if each.id == issue.id)
    assert memberships(second)[0] == ("money", issue.id)
    assert (joined.severity, joined.version) == ("high", 2)
    # high: 2 days, earlier than low's 14.
    assert joined.due_at <= utc_now() + timedelta(days=2) < issue.due_at
    assert (joined.first_seen_at, joined.last_seen_at) == (visited_at(first), visited_at(second))


def test_a_lower_severity_joining_moves_neither_the_severity_nor_the_due_date(
    org: Org_,
) -> None:
    first = confirmed(org, HIGH_PAYMENT)  # money, and field for a high payment
    money = memberships(first)[0][1]
    [before] = [issue for issue in issues_of(org) if issue.id == money]

    second = confirmed(org)  # low: money alone

    [after] = [issue for issue in issues_of(org) if issue.id == money]
    assert memberships(second) == [("money", money)]
    assert (after.severity, after.due_at, after.version) == ("high", before.due_at, 2)


def test_unverified_and_unrouted_insights_join_no_issue(org: Org_) -> None:
    demand = insight(category="demand", severity="low", suggested_team=None)  # no team: triage
    unfound = insight(evidence=[{"turn_id": 3, "quote": PARAPHRASE}])
    visit_id, _ = reviewed(org, demand, unfound, busy=True)  # the repair finds nothing

    confirm(org, visit_id)

    assert issues_of(org) == []
    assert memberships(visit_id) == []


def test_confirming_twice_files_nothing_twice(org: Org_) -> None:
    visit_id = confirmed(org)

    confirm(org, visit_id)

    [issue] = issues_of(org)
    assert issue.version == 1


# --- reopening -------------------------------------------------------------------------------


def test_a_matching_insight_within_the_window_reopens_the_resolved_issue(org: Org_) -> None:
    confirmed(org)
    [issue] = issues_of(org)
    resolve(issue.id, days_ago=29)
    before = utc_now()

    second = confirmed(org)

    [reopened] = issues_of(org)
    assert memberships(second) == [("money", issue.id)]
    assert (reopened.status, reopened.reopened_count, reopened.version) == ("open", 1, 2)
    assert (reopened.resolved_at, reopened.resolution_note) == (None, None)
    assert before + timedelta(days=14) <= reopened.due_at <= utc_now() + timedelta(days=14)
    with session_scope(read_only=True) as session:
        event = session.scalars(select(IssueEvent).where(IssueEvent.issue_id == issue.id)).one()
    assert (event.actor_id, event.field, event.from_value, event.to_value, event.note) == (
        org.rep_id,
        "status",
        "resolved",
        "open",
        None,
    )


def test_after_the_window_a_new_issue_starts(org: Org_) -> None:
    confirmed(org)
    [old] = issues_of(org)
    resolve(old.id, days_ago=31)

    second = confirmed(org)

    [still, new] = issues_of(org)
    assert (still.id, still.status) == (old.id, "resolved")
    assert (new.issue_key, new.status, new.reopened_count) == (old.issue_key, "open", 0)
    assert memberships(second) == [("money", new.id)]


def test_a_reopen_takes_the_new_insight_s_severity_not_the_old_one_s(org: Org_) -> None:
    first = confirmed(org, HIGH_PAYMENT)
    money = memberships(first)[0][1]
    assert money is not None
    resolve(money, days_ago=3)
    before = utc_now()

    confirmed(org)  # low: money alone

    [reopened] = [issue for issue in issues_of(org) if issue.id == money]
    assert (reopened.status, reopened.severity) == ("open", "low")
    assert before + timedelta(days=14) <= reopened.due_at <= utc_now() + timedelta(days=14)


def test_an_issue_two_routes_join_at_once_is_counted_and_changed_once(
    org: Org_, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=issues.__name__):
        visit_id = confirmed(org, insight(category="payments"), insight(category="payments"))

    [issue] = issues_of(org)
    assert memberships(visit_id) == [("money", issue.id), ("money", issue.id)]
    assert issue.version == 1
    logged = [r.getMessage() for r in caplog.records if r.name == issues.__name__]
    assert logged == [f"issues attached visit={visit_id} created=1 reopened=0 joined=0"]


@pytest.mark.parametrize(("field", "value"), [("category", "returns"), ("severity", "urgent")])
def test_a_key_the_playbook_no_longer_has_holds_the_confirm(
    org: Org_, field: str, value: str
) -> None:
    visit_id, [insight_id] = reviewed(org)
    with session_scope() as session:
        session.execute(update(Insight).where(Insight.id == insight_id).values({field: value}))

    with pytest.raises(Conflict) as refused:
        confirm(org, visit_id)

    assert refused.value.code == "playbook_changed"
    with session_scope(read_only=True) as session:
        assert session.get_one(Visit, visit_id).status == "review"
    assert issues_of(org) == []


def test_a_route_can_sit_only_in_its_own_team_s_issue(org: Org_) -> None:
    first = confirmed(org, HIGH_PAYMENT)  # money's issue and field's
    [(_, money), (_, field)] = memberships(first)

    with pytest.raises(IntegrityError) as refused, session_scope() as session:
        session.execute(
            update(InsightRoute).where(InsightRoute.issue_id == money).values(issue_id=field)
        )

    assert constraint_name(refused.value) == "fk_insight_routes_issue_id_issues"


def test_the_org_s_pack_is_read_through_the_session_it_s_given(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_second_session(**_: Any) -> None:
        raise AssertionError("a second session was opened")

    monkeypatch.setattr(extraction, "session_scope", no_second_session)
    with session_scope(read_only=True) as session:
        pack = extraction.pack_for(org.org_id, ORG_PACK_FIXTURES, PLAYBOOKS_DIR, session=session)

    assert (pack.id, pack.version) == ("tiny-demo", 1)


# --- two at once (§2.3) ----------------------------------------------------------------------


def test_two_confirms_making_the_same_issue_at_once_end_with_one(org: Org_) -> None:
    first, _ = reviewed(org)
    second, _ = reviewed(org)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        review.confirm(session, lambda: PACK, org.org_id, org.rep_id, first)  # made it, uncommitted
        other = pool.submit(confirm, org, second)
        wait_for(lambda: lock_waiters() == 1, "the second confirm never waited for the first")
        session.commit()

        other.result(timeout=10)
    [issue] = issues_of(org)
    assert memberships(first) == memberships(second) == [("money", issue.id)]
    assert issue.version == 2


def test_two_confirms_on_one_key_in_two_teams_both_finish(org: Org_) -> None:
    first, _ = reviewed(org, HIGH_PAYMENT)
    second, _ = reviewed(org, HIGH_PAYMENT)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        review.confirm(session, lambda: PACK, org.org_id, org.rep_id, first)  # holds both issues
        other = pool.submit(confirm, org, second)
        wait_for(lambda: lock_waiters() == 1, "the second confirm never waited for the first")
        session.commit()

        other.result(timeout=10)
    found = issues_of(org)
    assert len(found) == 2
    assert {issue_id for _, issue_id in memberships(second)} == {issue.id for issue in found}


# --- what the database holds -----------------------------------------------------------------


def an_issue(org: Org_, **changes: Any) -> dict[str, Any]:
    with session_scope(read_only=True) as session:
        team = session.scalars(select(Team.id).where(Team.org_id == org.org_id)).first()
    now = utc_now()
    return {
        "id": new_id(),
        "org_id": org.org_id,
        "team_id": team,
        "category": "payments",
        "issue_key": "payments|x",
        "key_fields": {"distributor": "x"},
        "title_en": "Credit and payments",
        "severity": "low",
        "due_at": now,
        "first_seen_at": now,
        "last_seen_at": now,
    } | changes


def refused(org: Org_, *rows: dict[str, Any]) -> str | None:
    """The constraint that refuses these issues, or None if they're all kept."""
    try:
        with session_scope() as session:
            for row in rows:
                session.add(Issue(**row))
                session.flush()
    except IntegrityError as error:
        return constraint_name(error)
    return None


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"status": "resolved", "resolution_note": "Done."}, "ck_issues_resolved_at"),
        ({"status": "resolved", "resolved_at": utc_now()}, "ck_issues_resolution_note"),
        ({"resolution_note": "Done."}, "ck_issues_resolution_note"),
        (
            {"status": "resolved", "resolved_at": utc_now(), "resolution_note": ""},
            "ck_issues_resolution_note_length",
        ),
        ({"status": "merged"}, "ck_issues_merged"),
        ({"status": "closed"}, "ck_issues_status"),
        ({"version": 0}, "ck_issues_counts"),
        ({"last_seen_at": utc_now() - timedelta(days=1)}, "ck_issues_seen"),
    ],
)
def test_the_database_refuses_an_issue_that_doesn_t_add_up(
    org: Org_, changes: dict[str, Any], constraint: str
) -> None:
    assert refused(org, an_issue(org, **changes)) == constraint


def test_an_issue_merged_into_itself_is_refused(org: Org_) -> None:
    row = an_issue(org, status="merged")
    row["merged_into_id"] = row["id"]

    assert refused(org, row) == "ck_issues_merged"


def test_one_open_issue_per_team_and_key_but_resolved_ones_may_repeat(org: Org_) -> None:
    resolved = {"status": "resolved", "resolved_at": utc_now(), "resolution_note": "Done."}

    assert refused(org, an_issue(org), an_issue(org, status="in_progress")) == (
        "uq_issues_active_key"
    )
    assert refused(org, an_issue(org, **resolved), an_issue(org, **resolved), an_issue(org)) is None


def test_the_issue_history_is_never_changed(org: Org_) -> None:
    confirmed(org)
    [issue] = issues_of(org)
    resolve(issue.id, days_ago=1)
    confirmed(org)  # reopens it: one event

    for statement in ("UPDATE issue_events SET note = 'x'", "DELETE FROM issue_events"):
        with pytest.raises(DBAPIError) as refused, session_scope() as session:
            session.execute(text(statement))
        assert refused.value.orig.sqlstate == "23001"  # type: ignore[union-attr]  # restrict_violation
