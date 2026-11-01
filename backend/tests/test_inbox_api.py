"""The inbox's API (batch 15): /api/issues (the team's cards, one issue, a change), the teams and
triage lists, and a team member's accept, Move, Also send and triage assignment. The demo rep's
org, on the PepsiCo example pack: payments go to finance, and to the area manager when high;
"other" has no team, so it waits in triage."""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from countertalk import extraction
from countertalk.db import session_scope
from countertalk.models import Distributor, InsightRoute, Shop, utc_now
from tests.test_extraction import answer, insight, transcribed
from tests.test_review_api import PAYMENT, error_of, seeded_demo, the_review
from tests.test_roles import ALL_PLACED, FakeLlm
from tests.test_visits import Org_

HIGH = insight(category="payments", severity="high", suggested_team="finance")
OTHER = insight(category="other", severity="low", suggested_team=None)  # no team: triage


@pytest.fixture(autouse=True)
def demo() -> Org_:
    return seeded_demo()


def shop(demo: Org_, name: str) -> Org_:
    """The demo org, with one of its shops by name."""
    with session_scope(read_only=True) as session:
        query = select(Shop.id).where(Shop.org_id == demo.org_id, Shop.name == name)
        return Org_(demo.org_id, demo.rep_id, session.scalars(query).one())


def distributor(demo: Org_, key: str) -> str:
    with session_scope(read_only=True) as session:
        query = select(Distributor.id).where(Distributor.org_id == demo.org_id)
        return session.scalars(query.where(Distributor.key == key)).one()


def confirmed(client: TestClient, at: Org_, *found: dict[str, Any]) -> str:
    """A visit at that shop with these insights, confirmed through the API: its ID."""
    visit_id = transcribed(at)
    extraction.extract(visit_id, FakeLlm(ALL_PLACED, answer(*(found or (PAYMENT,)))))
    response = client.post(f"/api/visits/{visit_id}/confirm")
    assert response.status_code == 200, response.text
    return visit_id


def inbox(client: TestClient, team: str, **filters: Any) -> list[dict[str, Any]]:
    response = client.get("/api/issues", params={"team": team, **filters})
    assert response.status_code == 200, response.text
    return response.json()


def issue(client: TestClient, issue_id: str) -> dict[str, Any]:
    response = client.get(f"/api/issues/{issue_id}")
    assert response.status_code == 200, response.text
    return response.json()


def team_id(client: TestClient, key: str) -> str:
    return next(t["id"] for t in client.get("/api/issues/teams").json() if t["key"] == key)


# --- the cards -------------------------------------------------------------------------------


def test_a_card_counts_shops_insights_and_reps(client: TestClient, demo: Org_) -> None:
    corner = shop(demo, "Corner Daily Needs")  # Station Road, d3

    confirmed(client, corner, HIGH)

    [card] = inbox(client, "finance")
    assert {k: card[k] for k in ("team_key", "category", "severity", "status", "version")} == {
        "team_key": "finance",
        "category": "payments",
        "severity": "high",
        "status": "open",
        "version": 1,
    }
    assert card["title_en"] == "Credit and payments · Station Road Distributors (example)"
    assert (card["shops"], card["insights"], card["reps"], card["raised_again"]) == (1, 1, 1, False)
    assert (card["overdue"], card["owner_id"], card["owner_name"]) == (False, None, None)
    # Two teams: one issue in each.
    [also] = inbox(client, "area_manager")
    assert (also["title_en"], also["id"] != card["id"]) == (card["title_en"], True)
    assert inbox(client, "distribution") == []


def test_one_shop_on_two_visits_counts_once_and_is_raised_again(
    client: TestClient, demo: Org_
) -> None:
    corner, moonlight = shop(demo, "Corner Daily Needs"), shop(demo, "Moonlight Provisions")
    confirmed(client, corner)
    confirmed(client, corner)

    [card] = inbox(client, "finance")
    assert (card["shops"], card["insights"], card["raised_again"]) == (1, 2, True)

    confirmed(client, moonlight)  # the same distributor, d3: the same issue

    [card] = inbox(client, "finance")
    assert (card["shops"], card["insights"], card["raised_again"]) == (2, 3, True)


def test_highest_severity_first_then_most_shops_then_filters(
    client: TestClient, demo: Org_
) -> None:
    confirmed(client, shop(demo, "Corner Daily Needs"))  # d3, low
    confirmed(client, shop(demo, "Moonlight Provisions"))  # d3, low: 2 shops
    confirmed(client, shop(demo, "Sunrise Kirana"), HIGH)  # d1, high: 1 shop
    confirmed(client, shop(demo, "Lotus General Store"))  # d2, low: 1 shop

    titles = [card["title_en"].split(" · ")[1] for card in inbox(client, "finance")]

    assert titles == [
        "Northgate Agencies (example)",
        "Station Road Distributors (example)",
        "Riverbend Traders (example)",
    ]
    old_market = [c["title_en"] for c in inbox(client, "finance", area="Old Market")]
    assert [title.split(" · ")[1] for title in old_market] == [
        "Northgate Agencies (example)",
        "Riverbend Traders (example)",
    ]
    by_d1 = inbox(client, "finance", distributor=distributor(demo, "d1"))
    assert [card["severity"] for card in by_d1] == ["high"]
    assert inbox(client, "finance", category="supply") == []


def test_the_team_is_a_query_parameter_and_must_be_one_of_the_org_s(
    client: TestClient, demo: Org_
) -> None:
    assert error_of(client.get("/api/issues", params={"team": "nobody"})) == (
        404,
        "team_not_found",
    )
    assert client.get("/api/issues").status_code == 422
    # The dropdown's teams, by name.
    assert [(t["key"], t["name"]) for t in client.get("/api/issues/teams").json()] == [
        ("area_manager", "Area manager"),
        ("distribution", "Distribution"),
        ("finance", "Finance"),
        ("sales_ops", "Sales operations"),
        ("trade_marketing", "Trade marketing"),
    ]


# --- one issue, and changing it --------------------------------------------------------------


def test_an_issue_shows_each_insight_with_its_shop_rep_quote_and_route(
    client: TestClient, demo: Org_
) -> None:
    visit_id = confirmed(client, shop(demo, "Corner Daily Needs"))
    [card] = inbox(client, "finance")

    found = issue(client, card["id"])

    assert found["issue"] == card
    [member] = found["members"]
    assert (member["shop_name"], member["area"], member["rep_name"], member["visit_id"]) == (
        "Corner Daily Needs",
        "Station Road",
        "Demo Rep",
        visit_id,
    )
    assert member["insight"]["quotes"][0]["start_ms"] is not None
    assert (member["route"]["team_key"], member["route"]["version"]) == ("finance", 1)
    assert member["route"]["issue_id"] == card["id"]


def test_finance_assigns_works_and_resolves_and_the_rep_sees_the_note(
    client: TestClient, demo: Org_
) -> None:
    visit_id = confirmed(client, shop(demo, "Corner Daily Needs"))
    [card] = inbox(client, "finance")
    url = f"/api/issues/{card['id']}"

    mine = client.patch(url, json={"version": 1, "assign_to_me": True})
    started = client.patch(url, json={"version": 2, "status": "in_progress"})
    done = client.patch(
        url, json={"version": 3, "status": "resolved", "note": "Credit note sent today."}
    )

    assert mine.json()["issue"]["owner_name"] == "Demo Rep"
    assert started.json()["issue"]["status"] == "in_progress"
    assert (done.json()["issue"]["status"], done.json()["issue"]["resolution_note"]) == (
        "resolved",
        "Credit note sent today.",
    )
    [shown] = the_review(client, visit_id)["insights"]
    assert [(r["team_key"], r["issue_status"], r["resolution_note"]) for r in shown["routes"]] == [
        ("finance", "resolved", "Credit note sent today.")
    ]
    assert inbox(client, "finance") == []
    assert [c["id"] for c in inbox(client, "finance", status="resolved")] == [card["id"]]


def test_a_route_confirmed_before_issues_shows_its_team_with_no_status(
    client: TestClient, demo: Org_
) -> None:
    visit_id = confirmed(client, shop(demo, "Corner Daily Needs"))
    with session_scope() as session:  # as 91bbef66's, confirmed in batch 14
        session.execute(update(InsightRoute).values(issue_id=None))

    [shown] = the_review(client, visit_id)["insights"]

    assert [(r["team_key"], r["issue_id"], r["issue_status"]) for r in shown["routes"]] == [
        ("finance", None, None)
    ]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"version": 0, "status": "in_progress"}, (409, "issue_changed")),
        ({"version": 1, "status": "resolved"}, (400, "note_required")),
        ({"version": 1, "status": "resolved", "note": " "}, (400, "note_required")),
        ({"version": 1, "due_at": (utc_now() + timedelta(days=3)).isoformat()}, (403, "not_owner")),
    ],
)
def test_a_change_that_can_t_be_made_is_refused_with_its_reason(
    client: TestClient, demo: Org_, body: dict[str, Any], expected: tuple[int, str]
) -> None:
    confirmed(client, shop(demo, "Corner Daily Needs"))
    [card] = inbox(client, "finance")

    assert error_of(client.patch(f"/api/issues/{card['id']}", json=body)) == expected
    assert inbox(client, "finance") == [card]


@pytest.mark.parametrize(
    "body",
    [
        {"status": "in_progress"},  # no version
        {"version": "1", "status": "in_progress"},  # a string isn't a version
        {"version": 1, "status": "merged"},
        {"version": 1, "assign_to_me": "yes"},
        {"version": 1, "due_at": "2026-12-01T10:00:00"},  # no time zone
        {"version": 1, "owner_id": "someone"},
    ],
)
def test_a_change_that_isn_t_one_is_422(
    client: TestClient, demo: Org_, body: dict[str, Any]
) -> None:
    confirmed(client, shop(demo, "Corner Daily Needs"))
    [card] = inbox(client, "finance")

    assert client.patch(f"/api/issues/{card['id']}", json=body).status_code == 422


# --- triage and routing ----------------------------------------------------------------------


def test_triage_lists_confirmed_insights_with_no_team_until_one_is_given(
    client: TestClient, demo: Org_
) -> None:
    visit_id = confirmed(client, shop(demo, "Corner Daily Needs"), OTHER)
    [waiting] = client.get("/api/issues/triage").json()
    assert (waiting["visit_id"], waiting["shop_name"], waiting["insight"]["routes"]) == (
        visit_id,
        "Corner Daily Needs",
        [],
    )
    url = f"/api/insights/{waiting['insight']['id']}/team"

    given = client.post(url, json={"team_id": team_id(client, "sales_ops")})
    again = client.post(url, json={"team_id": team_id(client, "finance")})

    assert given.status_code == 200, given.text
    [route] = given.json()["routes"]
    assert (route["team_key"], route["routed_by"], route["issue_status"]) == (
        "sales_ops",
        "person",
        "open",
    )
    assert error_of(again) == (409, "already_routed")
    assert client.get("/api/issues/triage").json() == []
    [card] = inbox(client, "sales_ops")
    assert card["title_en"] == "Other · Station Road"


def test_move_and_also_send_name_the_route_they_read(client: TestClient, demo: Org_) -> None:
    confirmed(client, shop(demo, "Corner Daily Needs"))
    [card] = inbox(client, "finance")
    [member] = issue(client, card["id"])["members"]
    insight_id = member["insight"]["id"]
    seen = {"slot": 1, "version": 1}

    sent = client.post(
        f"/api/insights/{insight_id}/also-send",
        json={**seen, "team_id": team_id(client, "sales_ops")},
    )
    moved = client.post(
        f"/api/insights/{insight_id}/move",
        json={**seen, "team_id": team_id(client, "distribution")},
    )
    stale = client.post(
        f"/api/insights/{insight_id}/move",
        json={**seen, "team_id": team_id(client, "trade_marketing")},
    )

    assert [r["team_key"] for r in sent.json()["routes"]] == ["finance", "sales_ops"]
    assert [(r["team_key"], r["routed_by"], r["version"]) for r in moved.json()["routes"]] == [
        ("distribution", "person", 2),
        ("sales_ops", "person", 1),
    ]
    assert error_of(stale) == (409, "route_changed")
    # finance's issue lost its only insight: hidden from the list, and not found.
    assert inbox(client, "finance") == []
    assert error_of(client.get(f"/api/issues/{card['id']}")) == (404, "issue_not_found")
    third = client.post(
        f"/api/insights/{insight_id}/also-send",
        json={"slot": 1, "version": 2, "team_id": team_id(client, "finance")},
    )
    assert error_of(third) == (409, "team_limit")


def test_accepting_the_ai_s_team_clears_suggested_by_ai(client: TestClient, demo: Org_) -> None:
    suggested = insight(category="other", severity="low", suggested_team="sales_ops")
    confirmed(client, shop(demo, "Corner Daily Needs"), suggested)
    [card] = inbox(client, "sales_ops")
    [member] = issue(client, card["id"])["members"]
    assert member["route"]["routed_by"] == "ai"

    accepted = client.post(
        f"/api/insights/{member['insight']['id']}/accept", json={"slot": 1, "version": 1}
    )

    assert [(r["routed_by"], r["version"]) for r in accepted.json()["routes"]] == [("person", 2)]
    assert error_of(
        client.post(
            f"/api/insights/{member['insight']['id']}/accept", json={"slot": 1, "version": 1}
        )
    ) == (409, "route_changed")
