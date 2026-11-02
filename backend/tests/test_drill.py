"""A team summary's drill-down (batch 15c): the issue's latest run, its reps, their shops and
visits, and each quote, counted as the summary counts them."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from countertalk import drill, issues, summary_jobs
from countertalk.db import session_scope
from countertalk.errors import NotFound
from countertalk.models import Distributor, Role, User, Visit, ist_date
from countertalk.summaries import period_of, team_numbers
from scripts import _k6
from tests.test_extraction import insight
from tests.test_inbox_api import confirmed, inbox, shop
from tests.test_issue_changes import now_of
from tests.test_merge import money_issue
from tests.test_merges import NOTHING, ByName, suggesting
from tests.test_narrative import at_shop, day
from tests.test_review import PACK
from tests.test_review_api import error_of, signed_in_manager
from tests.test_roles import tiny_org
from tests.test_summaries import EXPECTED, reopened_by_a_low_one, resolve, visit_at
from tests.test_summary_jobs import LAST_WEEK, summary_row, team
from tests.test_visits import Org_


def test_k6_s_p1_merged_then_summarised_drills_down_to_the_summary_s_own_numbers() -> None:
    world = _k6.load()
    finance = world.teams["finance"]
    p1, e = _k6.issue_of(world, world.insights["P1"][0], "finance"), _k6.expected_merge(world)[1]
    member = world.finance
    with session_scope() as session:  # 1. the merge
        issues.merge(
            session,
            lambda: world.pack,
            world.org_id,
            member,
            (p1, now_of(p1).version),
            (e, now_of(e).version),
        )
    with session_scope() as session:  # 2. the W6 summary, written
        summary_id = summary_jobs.ask(
            session, world.org_id, "team", finance, "week", world.week(7).start
        ).id
    summary_jobs.run(summary_id, ByName(merge_suggestions=suggesting(), summary_narrative=NOTHING))
    [written] = [i for i in summary_row(summary_id).numbers["issues"] if i["issue_id"] == p1]

    with session_scope(read_only=True) as session:  # 3. the drill-down
        found = drill.drill(session, world.org_id, finance, p1, world.week(6))
        d2 = session.scalars(
            select(Distributor.name).where(
                Distributor.org_id == world.org_id, Distributor.key == "d2"
            )
        ).one()

    after = EXPECTED["written"]["P1"]["after_merge"]
    assert (found.shops, found.insights, found.reps, found.raised_again) == (
        after["shops"],
        after["insights"],
        after["reps"],
        after["raised_again"],
    )
    measures = written["measures"]
    assert found.shops == measures["open_shops"]
    assert (
        found.in_period.shops,
        found.in_period.insights,
        found.in_period.reps,
        found.in_period.visits,
    ) == (measures["shops"], measures["insights"], measures["reps"], measures["visits"])
    every_shop = [s for rep in found.by_rep for s in rep.shops]
    assert ({s.distributor for s in every_shop}, len(every_shop)) == ({d2}, 5)
    assert [rep.rep_name for rep in found.by_rep] == sorted(rep.rep_name for rep in found.by_rep)
    assert sum(rep.insights for rep in found.by_rep) == found.insights
    quoted = [i for s in every_shop for v in s.visits for i in v.insights]
    assert all(view.quotes and view.quotes[0].start_ms is not None for view in quoted)


def test_another_team_s_issue_or_a_merged_one_is_404() -> None:
    world = _k6.load()
    p1, e = _k6.issue_of(world, world.insights["P1"][0], "finance"), _k6.expected_merge(world)[1]
    with session_scope() as session:
        issues.merge(
            session,
            lambda: world.pack,
            world.org_id,
            world.finance,
            (p1, now_of(p1).version),
            (e, now_of(e).version),
        )

    for key, issue_id in (("distribution", p1), ("finance", e)):
        with pytest.raises(NotFound), session_scope(read_only=True) as session:
            drill.drill(session, world.org_id, world.teams[key], issue_id, world.week(6))


# --- the API ---------------------------------------------------------------------------------


@pytest.fixture
def last_weeks(client: TestClient) -> tuple[Org_, str, str]:
    """A demo-org payment confirmed through the API, dated last Monday: the org, finance's
    team ID, and its issue's ID."""
    demo = signed_in_manager(client)
    visit_id = confirmed(client, shop(demo, "Corner Daily Needs"))
    with session_scope() as session:
        session.execute(
            update(Visit)
            .where(Visit.id == visit_id)
            .values(created_at=LAST_WEEK.start + timedelta(hours=6))
        )
    teams = client.get("/api/issues/options").json()["teams"]
    finance = next(t["id"] for t in teams if t["key"] == "finance")
    [card] = inbox(client, "finance")
    return demo, finance, card["id"]


def test_the_api_drills_down_to_each_quote_counted_now(
    client: TestClient, last_weeks: tuple[Org_, str, str]
) -> None:
    demo, finance, issue_id = last_weeks

    response = client.get(
        "/api/summaries/drill",
        params={"team_id": finance, "issue_id": issue_id, "period": "week"},
    )

    assert response.status_code == 200, response.text
    found = response.json()
    assert (found["issue_id"], found["shops"], found["insights"], found["reps"]) == (
        issue_id,
        1,
        1,
        1,
    )
    assert found["in_period"] == {"insights": 1, "shops": 1, "reps": 1, "visits": 1, "triage": 0}
    [rep] = found["by_rep"]
    [corner] = rep["shops"]
    [visit] = corner["visits"]
    assert (corner["name"], corner["distributor"], visit["in_period"]) == (
        "Corner Daily Needs",
        "Station Road Distributors (example)",
        True,
    )
    assert visit["insights"][0]["quotes"][0]["start_ms"] is not None
    assert found["numbers_as_of"] is not None


def test_the_api_s_drill_down_of_another_team_s_issue_is_404(
    client: TestClient, last_weeks: tuple[Org_, str, str]
) -> None:
    _, _, issue_id = last_weeks
    teams = client.get("/api/issues/options").json()["teams"]
    distribution = next(t["id"] for t in teams if t["key"] == "distribution")

    response = client.get(
        "/api/summaries/drill",
        params={"team_id": distribution, "issue_id": issue_id, "period": "week"},
    )

    assert error_of(response) == (404, "issue_not_found")


def test_a_reopened_issue_drills_down_to_its_run_since_the_reopen() -> None:
    org = tiny_org()
    issue_id, w3 = reopened_by_a_low_one(org)

    with session_scope(read_only=True) as session:
        found = drill.drill(
            session, org.org_id, team(org.org_id, "money"), issue_id, period_of("week", w3.date())
        )

    # The two shops' high payments came before the resolve: only the low one reopened it.
    assert (found.shops, found.insights, found.status) == (1, 1, "open")
    [rep] = found.by_rep
    [shop] = rep.shops
    assert [view.insight.severity for v in shop.visits for view in v.insights] == ["low"]


def test_reps_are_listed_by_name_whoever_came_first() -> None:
    org = tiny_org()
    with session_scope() as session:
        session.execute(update(User).where(User.id == org.rep_id).values(name="Zed (example)"))
        amy = User(org_id=org.org_id, email="amy@example.com", name="Amy (example)", role=Role.REP)
        session.add(amy)
        session.flush()
        later = Org_(org.org_id, amy.id, org.shop_id)
    payment = insight(category="payments", suggested_team="money")
    first = visit_at(org, day(0), day(0), payment)
    visit_at(later, day(1), day(1), payment)
    issue_id = money_issue(first)

    with session_scope(read_only=True) as session:
        found = drill.drill(session, org.org_id, team(org.org_id, "money"), issue_id, LAST_WEEK)

    assert [rep.rep_name for rep in found.by_rep] == ["Amy (example)", "Zed (example)"]


def test_a_resolve_and_reopen_inside_the_period_still_counts_as_the_summary_does() -> None:
    org = tiny_org()
    far = at_shop(org, "Far Shop", area="Test Area")
    high = insight(category="payments", severity="high", suggested_team="money")
    issue_id = money_issue(visit_at(org, day(0), day(0), high))
    visit_at(far, day(0), day(0), high)
    resolve(org, issue_id, day(1))
    visit_at(org, day(2), day(2))  # low, at the first shop: it reopens
    money = team(org.org_id, "money")

    with session_scope(read_only=True) as session:
        found = drill.drill(session, org.org_id, money, issue_id, LAST_WEEK)
        [numbers] = team_numbers(session, PACK, org.org_id, money, LAST_WEEK).issues

    m = numbers.measures
    assert (found.in_period.shops, found.in_period.insights, found.in_period.reps) == (
        m.shops,
        m.insights,
        m.reps,
    )
    assert (found.shops, found.raised_again) == (m.open_shops, m.raised_again)
    listed = [i for rep in found.by_rep for s in rep.shops for v in s.visits for i in v.insights]
    assert len(listed) == m.insights == 3


def test_an_issue_closed_before_the_period_with_nothing_in_it_is_404() -> None:
    org = tiny_org()
    week_before = summaries_period_before()
    issue_id = money_issue(visit_at(org, week_before, week_before))
    resolve(org, issue_id, week_before + timedelta(hours=2))

    with pytest.raises(NotFound), session_scope(read_only=True) as session:
        drill.drill(session, org.org_id, team(org.org_id, "money"), issue_id, LAST_WEEK)


def summaries_period_before() -> datetime:
    """A moment in the week before last week."""
    return LAST_WEEK.start - timedelta(days=5)


def test_the_api_drills_into_the_period_the_page_linked_and_refuses_another_day(
    client: TestClient, last_weeks: tuple[Org_, str, str]
) -> None:
    _, finance, issue_id = last_weeks
    monday = ist_date(LAST_WEEK.start)
    params = {"team_id": finance, "issue_id": issue_id, "period": "week"}

    linked = client.get("/api/summaries/drill", params={**params, "start": monday.isoformat()})
    tuesday = client.get(
        "/api/summaries/drill", params={**params, "start": (monday + timedelta(days=1)).isoformat()}
    )

    assert linked.status_code == 200, linked.text
    assert datetime.fromisoformat(linked.json()["period_start"]) == LAST_WEEK.start
    assert error_of(tuesday) == (400, "not_a_period_start")
