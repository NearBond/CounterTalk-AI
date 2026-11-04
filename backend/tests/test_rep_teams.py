"""The rep chooses each finding's team in review (batch F1): today's routing is the suggestion;
the rep changes the main team, adds or takes off a second, or leaves it for the manager. The
override's team stays. The demo org, on the PepsiCo example pack: payments go to finance, and to
the area manager too when high."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from countertalk.db import session_scope
from countertalk.models import RoutingDecision, Team
from tests.conftest import signed_in_as
from tests.test_extraction import insight
from tests.test_review_api import (
    PAYMENT,
    error_of,
    in_review,
    signed_in_demo,
    signed_in_manager,
    the_review,
)
from tests.test_roles import tiny_org
from tests.test_visits import Org_

HIGH = insight(category="payments", severity="high", suggested_team="finance")


@pytest.fixture
def demo(client: TestClient) -> Org_:
    return signed_in_demo(client)


def teams_of(demo: Org_) -> dict[str, str]:
    """The demo org's teams, key: id."""
    with session_scope(read_only=True) as session:
        query = select(Team.key, Team.id).where(Team.org_id == demo.org_id)
        return dict(session.execute(query).all())


def a_finding(client: TestClient, demo: Org_, *found: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A visit in review with this finding: its ID, and the finding as the review shows it."""
    visit_id = in_review(demo, *(found or (PAYMENT,)))
    [shown] = the_review(client, visit_id)["insights"]
    return visit_id, shown


def choose(client: TestClient, shown: dict[str, Any], *team_ids: str) -> Any:
    body = {"team_ids": list(team_ids), "routing_version": shown["routing_version"]}
    return client.put(f"/api/insights/{shown['id']}/teams", json=body)


def chosen(client: TestClient, shown: dict[str, Any], *team_ids: str) -> dict[str, Any]:
    response = choose(client, shown, *team_ids)
    assert response.status_code == 200, response.text
    return response.json()


def routes(found: dict[str, Any]) -> list[tuple[str, str]]:
    return [(route["team_key"], route["routed_by"]) for route in found["routes"]]


def test_the_review_pre_fills_the_suggestion_and_offers_the_org_s_teams(
    client: TestClient, demo: Org_
) -> None:
    visit_id, shown = a_finding(client, demo)

    assert routes(shown) == [("finance", "table")]
    assert (shown["suggested_team"], shown["chosen_by_rep"], shown["left_by_rep"]) == (
        "Finance",
        False,
        False,
    )
    assert [team["key"] for team in the_review(client, visit_id)["teams"]] == [
        "area_manager",
        "distribution",
        "finance",
        "sales_ops",
        "trade_marketing",
    ]


def test_the_rep_changes_the_main_team_and_adds_a_second(client: TestClient, demo: Org_) -> None:
    teams = teams_of(demo)
    _, shown = a_finding(client, demo)

    found = chosen(client, shown, teams["sales_ops"], teams["distribution"])

    assert routes(found) == [("sales_ops", "rep"), ("distribution", "rep")]
    assert (found["suggested_team"], found["chosen_by_rep"]) == ("Finance", True)
    assert found["routing_version"] == shown["routing_version"] + 2
    with session_scope(read_only=True) as session:
        history = session.execute(
            select(RoutingDecision.action, RoutingDecision.decided_by, RoutingDecision.team_id)
            .where(RoutingDecision.insight_id == shown["id"])
            .order_by(RoutingDecision.created_at)
        ).all()
    # The rule's suggestion is kept; the rep's choice comes after it, in their name.
    assert [(action, by) for action, by, _ in history] == [
        ("route", None),
        ("rep_choice", demo.rep_id),
        ("rep_choice", demo.rep_id),
    ]
    assert history[0][2] == teams["finance"]


def test_the_suggestion_chosen_again_isn_t_the_rep_s_own_choice(
    client: TestClient, demo: Org_
) -> None:
    teams = teams_of(demo)
    _, shown = a_finding(client, demo)
    moved = chosen(client, shown, teams["sales_ops"])

    back = chosen(client, moved, teams["finance"])

    assert routes(back) == [("finance", "table")]
    assert back["chosen_by_rep"] is False


def test_left_for_the_manager_it_goes_to_triage_marked_and_isn_t_deleted(
    client: TestClient, demo: Org_
) -> None:
    visit_id, shown = a_finding(client, demo)

    left = chosen(client, shown)

    assert (left["routes"], left["left_by_rep"], left["chosen_by_rep"]) == ([], True, True)
    assert client.post(f"/api/visits/{visit_id}/confirm").status_code == 200
    signed_in_manager(client)
    [waiting] = client.get("/api/issues/triage").json()
    assert (waiting["insight"]["id"], waiting["insight"]["left_by_rep"]) == (shown["id"], True)


def test_the_override_s_team_stays(client: TestClient, demo: Org_) -> None:
    teams = teams_of(demo)
    _, shown = a_finding(client, demo, HIGH)
    assert routes(shown) == [("finance", "table"), ("area_manager", "override")]
    assert shown["locked_team_id"] == teams["area_manager"]  # the picker shows it locked

    assert error_of(choose(client, shown, teams["sales_ops"])) == (400, "override_locked")
    assert error_of(choose(client, shown)) == (400, "override_locked")  # no leaving it, either
    found = chosen(client, shown, teams["sales_ops"], teams["area_manager"])

    assert routes(found) == [("sales_ops", "rep"), ("area_manager", "override")]
    assert found["locked_team_id"] == teams["area_manager"]  # the answer carries the lock too
    # Only the override's team: the main team taken off is the rep's own choice too.
    alone = chosen(client, found, teams["area_manager"])
    assert (routes(alone), alone["chosen_by_rep"]) == ([("area_manager", "override")], True)


def test_more_than_two_teams_a_team_twice_or_another_org_s_team_is_400(
    client: TestClient, demo: Org_
) -> None:
    teams = teams_of(demo)
    _, shown = a_finding(client, demo)
    with session_scope(read_only=True) as session:
        theirs = session.scalars(select(Team.id).where(Team.org_id == tiny_org().org_id)).first()
    assert theirs is not None

    assert error_of(choose(client, shown, *list(teams.values())[:3])) == (400, "too_many_teams")
    assert error_of(choose(client, shown, teams["finance"], teams["finance"])) == (
        400,
        "team_twice",
    )
    assert error_of(choose(client, shown, theirs)) == (400, "unknown_team")


def test_a_choice_made_on_an_old_read_is_409(client: TestClient, demo: Org_) -> None:
    teams = teams_of(demo)
    _, shown = a_finding(client, demo)
    chosen(client, shown, teams["sales_ops"])

    assert error_of(choose(client, shown, teams["distribution"])) == (409, "route_changed")


def test_after_confirm_the_teams_can_t_be_changed(client: TestClient, demo: Org_) -> None:
    teams = teams_of(demo)
    visit_id, shown = a_finding(client, demo)
    assert client.post(f"/api/visits/{visit_id}/confirm").status_code == 200

    assert error_of(choose(client, shown, teams["sales_ops"])) == (409, "visit_confirmed")


def test_an_unverified_finding_has_no_teams_to_choose(client: TestClient, demo: Org_) -> None:
    teams = teams_of(demo)
    unfound = insight(evidence=[{"turn_id": 3, "quote": "Kuch aur hi kaha tha."}])
    # Its quote's repair is asked for too (busy): a quote not found in the turn.
    [shown] = the_review(client, in_review(demo, unfound, busy=True))["insights"]
    assert shown["verified"] is False

    assert error_of(choose(client, shown, teams["sales_ops"])) == (409, "insight_unverified")


def test_a_category_edit_resets_the_teams_to_its_new_suggestion(
    client: TestClient, demo: Org_
) -> None:
    teams = teams_of(demo)
    _, shown = a_finding(client, demo)
    chosen(client, shown, teams["sales_ops"])

    response = client.patch(f"/api/insights/{shown['id']}", json={"category": "supply"})

    assert response.status_code == 200, response.text
    found = response.json()
    assert routes(found) == [("distribution", "table")]
    assert (found["suggested_team"], found["chosen_by_rep"]) == ("Distribution", False)


def test_after_confirm_the_chosen_team_has_it_with_its_suggestion(
    client: TestClient, demo: Org_
) -> None:
    teams = teams_of(demo)
    visit_id, shown = a_finding(client, demo)
    chosen(client, shown, teams["sales_ops"])
    with signed_in_as(client, demo.rep_id):
        assert client.post(f"/api/visits/{visit_id}/confirm").status_code == 200

    signed_in_manager(client)
    [card] = client.get("/api/issues", params={"team": "sales_ops"}).json()
    [member] = client.get(f"/api/issues/{card['id']}").json()["members"]

    assert (member["insight"]["suggested_team"], member["insight"]["chosen_by_rep"]) == (
        "Finance",
        True,
    )
    assert client.get("/api/issues", params={"team": "finance"}).json() == []
