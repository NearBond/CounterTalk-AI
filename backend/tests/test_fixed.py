"""Fixed since the last visit (batch 17b): a shop's issues resolved since anyone last visited it,
with the team's note, on the rep's Updates page (batch 17e). Never a rep's name or a quote."""

from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from countertalk import consent, fixed, issues
from countertalk.db import session_scope
from countertalk.models import (
    Insight,
    InsightRoute,
    Issue,
    IssueStatus,
    Role,
    Shop,
    Team,
    ist_date,
    utc_now,
)
from countertalk.summaries import period_of
from tests.conftest import a_user, sign_in
from tests.test_extraction import insight
from tests.test_review import PACK
from tests.test_roles import tiny_org
from tests.test_summaries import resolve, reviewed, visit_at
from tests.test_visits import Org_, new_visit

DAY = timedelta(days=1)
NOW = utc_now()
KEYS = {"team", "category", "problem", "note", "resolved_at"}


@pytest.fixture
def org(client: TestClient) -> Org_:
    found = tiny_org()
    sign_in(client, found.rep_id)
    return found


def raised(org: Org_, days_ago: float, **said: Any) -> str:
    """A visit at the org's shop days ago, confirmed then, with this insight: its ID."""
    when = NOW - days_ago * DAY
    return visit_at(org, when, when, insight(**said))


def issues_of(visit_id: str) -> dict[str, str]:
    """The visit's insights' issues, by their team's key."""
    with session_scope(read_only=True) as session:
        return dict(
            session.execute(
                select(Team.key, InsightRoute.issue_id)
                .join(Team, Team.id == InsightRoute.team_id)
                .join(Insight, Insight.id == InsightRoute.insight_id)
                .where(Insight.visit_id == visit_id)
            ).all()
        )


def listed(client: TestClient, shop_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/shops/{shop_id}/fixed")
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    return body["items"]


def another_shop(org: Org_, name: str) -> Org_:
    with session_scope() as session:
        shop = Shop(org_id=org.org_id, name=name, area="Test Area")
        session.add(shop)
        session.flush()
        consent.mark_unanswered(session, org.org_id, org.rep_id, "hi")
        return Org_(org.org_id, org.rep_id, shop.id)


def test_a_problem_fixed_since_the_last_visit_is_listed_with_its_note(
    client: TestClient, org: Org_
) -> None:
    visit = raised(org, 5, category="payments", severity="low", summary_en="The payment is late.")
    resolve(org, issues_of(visit)["money"], NOW - 2 * DAY)

    [item] = listed(client, org.shop_id)

    assert set(item) == KEYS  # never a rep's name or a quote
    assert {k: item[k] for k in KEYS - {"resolved_at"}} == {
        "team": "Money desk",
        "category": "payments",
        "problem": "The payment is late.",
        "note": "Paid.",
    }
    assert datetime.fromisoformat(item["resolved_at"]) == NOW - 2 * DAY
    assert "Kurkure ke do dabbe" not in client.get(f"/api/shops/{org.shop_id}/fixed").text


def test_one_fixed_before_the_last_visit_isnt_listed(client: TestClient, org: Org_) -> None:
    visit = raised(org, 5, category="payments", severity="low")
    resolve(org, issues_of(visit)["money"], NOW - 4 * DAY)
    raised(org, 3, category="supply", severity="low")  # the shop's last visit, after the fix

    assert listed(client, org.shop_id) == []


def test_an_issue_open_again_isnt_listed(client: TestClient, org: Org_) -> None:
    visit = raised(org, 5, category="payments", severity="low")
    issue = issues_of(visit)["money"]
    resolve(org, issue, NOW - 4 * DAY)
    # Raised again at another shop since: a new finding reopens it.
    elsewhere = another_shop(org, "Another Shop")
    when = NOW - 2 * DAY
    visit_at(elsewhere, when, when, insight(category="payments", severity="low"))

    with session_scope(read_only=True) as session:
        assert session.get_one(Issue, issue).status == IssueStatus.OPEN
    assert listed(client, org.shop_id) == []


def test_an_insight_routed_to_two_teams_is_under_each_team_s_issue(
    client: TestClient, org: Org_
) -> None:
    # High payments: the money desk's, and the field team's too (tiny-demo's override).
    visit = raised(org, 5, category="payments", severity="high")
    teams = issues_of(visit)
    resolve(org, teams["money"], NOW - 3 * DAY)
    resolve(org, teams["field"], NOW - 2 * DAY)

    assert [item["team"] for item in listed(client, org.shop_id)] == ["Field team", "Money desk"]


def test_the_problem_is_the_shop_s_latest_insight_in_the_issue(
    client: TestClient, org: Org_
) -> None:
    first = raised(org, 6, category="payments", severity="low", summary_en="Paid late once.")
    raised(org, 4, category="payments", severity="low", summary_en="Paid late again.")
    resolve(org, issues_of(first)["money"], NOW - 2 * DAY)

    assert [item["problem"] for item in listed(client, org.shop_id)] == ["Paid late again."]


def test_a_shop_whose_only_visit_is_today_s_recording_lists_nothing(
    client: TestClient, org: Org_
) -> None:
    elsewhere = raised(another_shop(org, "Elsewhere Shop"), 5, category="payments", severity="low")
    resolve(org, issues_of(elsewhere)["money"], NOW - 2 * DAY)
    new_visit(org)  # today's, still recording

    assert listed(client, org.shop_id) == []


def test_a_visit_earlier_today_isnt_the_last_visit(client: TestClient, org: Org_) -> None:
    visit = raised(org, 5, category="payments", severity="low")
    resolve(org, issues_of(visit)["money"], NOW - 2 * DAY)
    now = utc_now()  # at the test, not the import: the server reads its day then too
    visit_at(org, now, now, insight(category="supply", severity="low"))  # earlier today

    assert [item["category"] for item in listed(client, org.shop_id)] == ["payments"]


def test_the_newest_come_first_at_most_the_limit(
    client: TestClient, org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    money = raised(org, 6, category="payments", severity="low")
    field = raised(org, 5, category="supply", severity="low")
    resolve(org, issues_of(money)["money"], NOW - 2 * DAY)
    resolve(org, issues_of(field)["field"], NOW - 1 * DAY)
    monkeypatch.setattr(fixed, "FIXED_MAX", 1)

    assert [item["category"] for item in listed(client, org.shop_id)] == ["supply"]


def test_a_shop_that_isnt_one_is_404(client: TestClient, org: Org_) -> None:
    response = client.get("/api/shops/00000000-0000-4000-8000-000000000000/fixed")

    assert (response.status_code, response.json()["error"]["code"]) == (404, "shop_not_found")


# --- every shop's, for the rep's Updates page (batch 17e) -------------------------------------


def everywhere(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/me/fixed")
    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None
    return body["items"]


def test_updates_lists_each_shop_with_a_fix_its_newest_fix_first(
    client: TestClient, org: Org_
) -> None:
    here = raised(org, 5, category="payments", severity="low")
    resolve(org, issues_of(here)["money"], NOW - 3 * DAY)
    second = another_shop(org, "Second Shop")
    there = raised(second, 5, category="supply", severity="low")
    resolve(org, issues_of(there)["field"], NOW - 1 * DAY)
    another_shop(org, "Quiet Shop")  # nothing fixed: not listed

    found = everywhere(client)

    assert [shop["shop_id"] for shop in found] == [second.shop_id, org.shop_id]
    assert set(found[0]) == {"shop_id", "name", "area", "items"}
    assert (found[0]["name"], found[0]["area"]) == ("Second Shop", "Test Area")
    [item] = found[0]["items"]
    assert set(item) == KEYS  # never a rep's name or a quote
    assert (item["team"], item["category"]) == ("Field team", "supply")


def test_each_shop_counts_from_its_own_last_visit(client: TestClient, org: Org_) -> None:
    visit = raised(org, 5, category="payments", severity="low")
    resolve(org, issues_of(visit)["money"], NOW - 2 * DAY)
    raised(another_shop(org, "Second Shop"), 1, category="supply", severity="low")  # just a visit

    assert [shop["shop_id"] for shop in everywhere(client)] == [org.shop_id]


def test_a_shop_s_own_later_visit_hides_its_earlier_fix(client: TestClient, org: Org_) -> None:
    here = raised(org, 6, category="payments", severity="low")
    resolve(org, issues_of(here)["money"], NOW - 2 * DAY)  # after its last visit: listed
    second = another_shop(org, "Second Shop")
    there = raised(second, 5, category="supply", severity="low")
    resolve(org, issues_of(there)["field"], NOW - 3 * DAY)
    raised(second, 1, category="demand", severity="low")  # its last visit, after its fix

    assert [shop["shop_id"] for shop in everywhere(client)] == [org.shop_id]


# --- the rep's home (batch F2) ---------------------------------------------------------------


def test_a_rep_s_home_counts_their_own_visits_to_review_and_the_shops_to_pass_on(
    client: TestClient, org: Org_
) -> None:
    reviewed(org, insight())  # theirs, waiting for review
    other_rep = Org_(org.org_id, a_user(org.org_id, Role.REP, "Other Rep"), org.shop_id)
    reviewed(other_rep, insight())  # another rep's: never counted
    visit = raised(org, 5, category="payments", severity="low")  # confirmed: not to review
    resolve(org, issues_of(visit)["money"], NOW - 2 * DAY)  # its shop has a fix to pass on

    response = client.get("/api/me/home")

    assert response.status_code == 200
    assert response.json() == {"to_review": 1, "to_pass_on": 1}


def test_the_home_lists_only_the_rep_s_visits_in_review_however_many_others(
    client: TestClient, org: Org_
) -> None:
    waiting, _ = reviewed(org, insight())
    raised(org, 2, category="payments", severity="low")  # confirmed: not in review

    response = client.get("/api/visits", params={"status": "review"})

    assert response.status_code == 200
    assert [visit["id"] for visit in response.json()["items"]] == [waiting]


def test_a_rep_s_pending_are_their_own_problems_still_open_or_in_progress(
    client: TestClient, org: Org_
) -> None:
    supply = raised(org, 3, kind="problem", category="supply", severity="low")
    raised(org, 2, kind="product_request", category="payments", severity="low")  # no problem
    other_rep = Org_(org.org_id, a_user(org.org_id, Role.REP, "Other Rep"), org.shop_id)
    raised(other_rep, 1, kind="problem", category="payments", severity="low")  # not theirs
    issue = issues_of(supply)["field"]

    def pending() -> list[dict[str, Any]]:
        response = client.get("/api/me/pending")
        assert response.status_code == 200
        return response.json()["items"]

    [row] = pending()
    assert set(row) == {"issue_id", "shop", "area", "team", "problem", "status", "raised_at"}
    assert row["issue_id"] == issue
    assert (row["team"], row["status"]) == ("Field team", "open")
    with session_scope() as session:
        found = session.get_one(Issue, issue)
        issues.change(session, org.org_id, org.rep_id, issue, found.version, status="in_progress")
    assert [row["status"] for row in pending()] == ["in_progress"]
    resolve(org, issue, NOW)
    assert pending() == []


# --- the rep's own counts this month -----------------------------------------------------------


def month_start() -> datetime:
    """This IST month's, when the test runs: the server reads its month then too."""
    return period_of("month", ist_date(utc_now())).start


def counts(client: TestClient) -> dict[str, int]:
    response = client.get("/api/me/counts")
    assert response.status_code == 200
    return response.json()


def problem_at(org: Org_, when: datetime, **said: Any) -> str:
    return visit_at(org, when, when, insight(kind="problem", **said))


def test_a_rep_s_counts_are_their_problems_this_month_and_how_many_are_fixed(
    client: TestClient, org: Org_
) -> None:
    early = month_start() + timedelta(hours=1)
    fixed_one = problem_at(org, early, category="payments", severity="low")
    problem_at(org, early, category="supply", severity="low")  # still open
    resolve(org, issues_of(fixed_one)["money"], NOW)

    assert counts(client) == {"raised": 2, "fixed": 1}


def test_only_the_rep_s_own_problems_this_month_count(client: TestClient, org: Org_) -> None:
    early = month_start() + timedelta(hours=1)
    problem_at(org, month_start() - timedelta(hours=1), category="payments", severity="low")
    visit_at(org, early, early, insight(kind="product_request", category="demand"))
    other_rep = Org_(org.org_id, a_user(org.org_id, Role.REP, "Other Rep"), org.shop_id)
    problem_at(other_rep, early, category="payments", severity="low")

    assert counts(client) == {"raised": 0, "fixed": 0}


def test_a_problem_is_fixed_only_when_every_team_s_issue_is(client: TestClient, org: Org_) -> None:
    early = month_start() + timedelta(hours=1)
    visit = problem_at(org, early, category="payments", severity="high")  # money and field
    resolve(org, issues_of(visit)["money"], NOW)

    assert counts(client) == {"raised": 1, "fixed": 0}
    resolve(org, issues_of(visit)["field"], NOW)
    assert counts(client) == {"raised": 1, "fixed": 1}


def test_the_playbook_has_the_problem_kind_the_counts_ask_for() -> None:
    assert fixed.PROBLEM_KIND in PACK.playbook.kinds
