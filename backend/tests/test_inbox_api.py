"""The inbox's API (batch 15): /api/issues (the team's cards, one issue, a change), the teams and
triage lists, and a team member's accept, Move, Also send and triage assignment. The demo rep's
org, on the PepsiCo example pack: payments go to finance, and to the area manager when high;
"other" has no team, so it waits in triage."""

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from countertalk import extraction
from countertalk.db import session_scope
from countertalk.inbox import periods
from countertalk.limits import PERIOD_FIRST_YEAR, PERIOD_LAST_YEAR
from countertalk.models import (
    CatalogItem,
    Distributor,
    Insight,
    InsightRoute,
    Org,
    Shop,
    Visit,
    ist_date,
    utc_now,
)
from countertalk.summaries import at_midnight
from tests.conftest import signed_in_as
from tests.test_extraction import answer, insight, transcribed
from tests.test_review_api import PAYMENT, error_of, signed_in_manager, the_review
from tests.test_roles import ALL_PLACED, FakeLlm
from tests.test_visits import Org_

HIGH = insight(category="payments", severity="high", suggested_team="finance")
OTHER = insight(category="other", severity="low", suggested_team=None)  # no team: triage


@pytest.fixture(autouse=True)
def demo(client: TestClient) -> Org_:
    return signed_in_manager(client)


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
    with signed_in_as(client, at.rep_id):  # the visit's rep confirms it
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
    teams = client.get("/api/issues/options").json()["teams"]
    return next(t["id"] for t in teams if t["key"] == key)


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
    # A manager names the team; a team member's is their own (test_access).
    assert error_of(client.get("/api/issues")) == (400, "team_required")


def test_the_options_are_the_org_s_teams_categories_areas_and_distributors(
    client: TestClient, demo: Org_
) -> None:
    found = client.get("/api/issues/options").json()

    # The dropdown's teams, by name.
    assert [(t["key"], t["name"]) for t in found["teams"]] == [
        ("area_manager", "Area manager"),
        ("distribution", "Distribution"),
        ("finance", "Finance"),
        ("sales_ops", "Sales operations"),
        ("trade_marketing", "Trade marketing"),
    ]
    assert {"key": "payments", "label": "Credit and payments"} in found["categories"]
    assert found["categories"][-1] == {"key": "other", "label": "Other"}
    assert found["areas"] == [
        "Lake View",
        "Mill Road",
        "Old Market",
        "Park Road",
        "Riverside",
        "Station Road",
    ]
    assert [d["name"] for d in found["distributors"]] == [
        "Northgate Agencies (example)",
        "Riverbend Traders (example)",
        "Station Road Distributors (example)",
    ]
    assert found["distributors"][0]["id"] == distributor(demo, "d1")


# --- competitor, product and period (batch 17c) ----------------------------------------------

UNLINKED = insight(category="payments", severity="low", suggested_team="finance", sku_mentions=[])
OCT_5 = date(2026, 10, 5)


def item(demo: Org_, key: str) -> str:
    """A catalog item of the demo org's, by its key."""
    with session_scope(read_only=True) as session:
        query = select(CatalogItem.id).where(CatalogItem.org_id == demo.org_id)
        return session.scalars(query.where(CatalogItem.key == key)).one()


def change_visit(visit_id: str, when: datetime | None = None, **links: str) -> None:
    """When the visit happened, and its insights' catalog links."""
    with session_scope() as session:
        if links:
            session.execute(update(Insight).where(Insight.visit_id == visit_id).values(**links))
        if when is not None:
            session.execute(update(Visit).where(Visit.id == visit_id).values(created_at=when))


def distributors(client: TestClient, **filters: str) -> list[str]:
    """Finance's cards, by their distributor."""
    return [card["title_en"].split(" · ")[1] for card in inbox(client, "finance", **filters)]


def test_a_competitor_or_product_keeps_the_issues_with_an_insight_linked_to_it(
    client: TestClient, demo: Org_
) -> None:
    corner = confirmed(client, shop(demo, "Corner Daily Needs"), UNLINKED)  # d3
    confirmed(client, shop(demo, "Moonlight Provisions"), UNLINKED)  # d3 too, unlinked
    sunrise = confirmed(client, shop(demo, "Sunrise Kirana"), UNLINKED)  # d1
    haldirams, lays = item(demo, "haldirams"), item(demo, "lays")
    change_visit(corner, competitor_id=haldirams)
    change_visit(sunrise, sku_id=lays)
    every = {card["id"]: card for card in inbox(client, "finance")}

    [rival] = inbox(client, "finance", competitor=haldirams)
    [own] = inbox(client, "finance", product=lays)

    # The filter picks issues: its card's numbers are the whole issue's.
    assert rival == every[rival["id"]]
    assert (rival["shops"], rival["insights"]) == (2, 2)
    assert own["title_en"] == "Credit and payments · Northgate Agencies (example)"
    assert inbox(client, "finance", competitor=haldirams, product=lays) == []


def test_a_brand_takes_in_its_products_and_a_product_is_itself_alone(
    client: TestClient, demo: Org_
) -> None:
    brand = confirmed(client, shop(demo, "Sunrise Kirana"), UNLINKED)  # d1
    masala = confirmed(client, shop(demo, "Corner Daily Needs"), UNLINKED)  # d3
    salted = confirmed(client, shop(demo, "Lotus General Store"), UNLINKED)  # d2
    change_visit(brand, sku_id=item(demo, "lays"))
    change_visit(masala, sku_id=item(demo, "lays_magic_masala"))
    change_visit(salted, sku_id=item(demo, "lays_classic_salted"))

    assert sorted(distributors(client, product=item(demo, "lays"))) == [
        "Northgate Agencies (example)",
        "Riverbend Traders (example)",
        "Station Road Distributors (example)",
    ]
    assert distributors(client, product=item(demo, "lays_magic_masala")) == [
        "Station Road Distributors (example)"
    ]


def test_a_brand_is_offered_when_only_its_products_are_linked(
    client: TestClient, demo: Org_
) -> None:
    masala = confirmed(client, shop(demo, "Corner Daily Needs"), UNLINKED)
    change_visit(masala, sku_id=item(demo, "lays_magic_masala"))

    found = client.get("/api/issues/options").json()

    assert [product["name"] for product in found["products"]] == [
        "Lay's",
        "Lay's India's Magic Masala",
    ]


def test_a_period_keeps_the_issues_with_an_insight_from_a_visit_in_it(
    client: TestClient, demo: Org_
) -> None:
    october = confirmed(client, shop(demo, "Sunrise Kirana"), UNLINKED)  # d1
    confirmed(client, shop(demo, "Corner Daily Needs"), UNLINKED)  # d3, today
    change_visit(october, at_midnight(OCT_5))  # 5 Oct's first moment in IST

    assert distributors(client, **{"from": "2026-10-05", "to": "2026-10-05"}) == [
        "Northgate Agencies (example)"
    ]
    assert distributors(client, to="2026-10-04") == []  # 4 Oct ends as 5 Oct starts
    assert distributors(client, **{"from": "2026-10-06"}) == ["Station Road Distributors (example)"]


def test_the_filters_are_met_by_one_insight_together(client: TestClient, demo: Org_) -> None:
    # One issue (d3): a Haldiram's insight from 5 Oct, and one from today linked to none.
    corner = confirmed(client, shop(demo, "Corner Daily Needs"), UNLINKED)
    confirmed(client, shop(demo, "Moonlight Provisions"), UNLINKED)
    haldirams = item(demo, "haldirams")
    change_visit(corner, at_midnight(OCT_5), competitor_id=haldirams)
    today = ist_date(utc_now()).isoformat()

    assert inbox(client, "finance", competitor=haldirams, **{"from": today, "to": today}) == []
    october = {"from": "2026-10-05", "to": "2026-10-05"}
    assert len(inbox(client, "finance", competitor=haldirams, **october)) == 1


def test_a_catalog_item_that_isn_t_the_org_s_of_that_kind_is_400(
    client: TestClient, demo: Org_
) -> None:
    with session_scope() as session:
        other = Org(name="Other Org")
        session.add(other)
        session.flush()
        theirs = CatalogItem(
            org_id=other.id, key="rival", kind="competitor", company="Rival", brand="Rival"
        )
        session.add(theirs)
        session.flush()
        theirs_id = theirs.id
    haldirams, lays = item(demo, "haldirams"), item(demo, "lays")
    nothing = "00000000-0000-4000-8000-000000000000"

    for asked, code in [
        ({"competitor": lays}, "unknown_competitor"),  # a product given as a competitor
        ({"product": haldirams}, "unknown_product"),
        ({"competitor": theirs_id}, "unknown_competitor"),
        ({"product": nothing}, "unknown_product"),
    ]:
        response = client.get("/api/issues", params={"team": "finance", **asked})
        assert error_of(response) == (400, code), asked
    response = client.get("/api/issues", params={"team": "finance", "competitor": lays})
    assert response.json()["error"]["message"] == "That isn't one of the competitors in the catalog"


def test_a_period_that_ends_before_it_starts_is_400(client: TestClient, demo: Org_) -> None:
    backwards = {"team": "finance", "from": "2026-11-02", "to": "2026-11-01"}

    assert error_of(client.get("/api/issues", params=backwards)) == (400, "period_backwards")
    assert inbox(client, "finance", **{"from": "2026-11-01", "to": "2026-11-01"}) == []


OUT_OF_RANGE = [{"from": "0001-01-01"}, {"to": "9999-12-31"}, {"to": "1999-12-31"}]


@pytest.mark.parametrize("asked", OUT_OF_RANGE)
def test_a_period_day_outside_the_years_it_takes_is_400(
    client: TestClient, demo: Org_, asked: dict[str, str]
) -> None:
    response = client.get("/api/issues", params={"team": "finance", **asked})

    assert error_of(response) == (400, "period_out_of_range")
    assert response.json()["error"]["message"] == (
        f"A period's days are from {PERIOD_FIRST_YEAR} to {PERIOD_LAST_YEAR}"
    )


def test_the_options_offer_competitors_and_products_in_issues_and_the_periods(
    client: TestClient, demo: Org_
) -> None:
    corner = confirmed(client, shop(demo, "Corner Daily Needs"), UNLINKED)
    navratan, lays = item(demo, "haldirams_navratan"), item(demo, "lays")
    change_visit(corner, competitor_id=navratan, sku_id=lays)
    # An unconfirmed visit's insight is routed, but in no issue yet.
    unconfirmed = transcribed(shop(demo, "Sunrise Kirana"))
    extraction.extract(unconfirmed, FakeLlm(ALL_PLACED, answer(UNLINKED)))
    change_visit(unconfirmed, competitor_id=item(demo, "bingo"))

    found = client.get("/api/issues/options").json()

    # Only what some issue's insight links to (a brand, too, through its product): never the
    # whole catalog.
    assert found["competitors"] == [
        {"id": item(demo, "haldirams"), "name": "Haldiram's"},
        {"id": navratan, "name": "Haldiram's Navratan Mix"},
    ]
    assert found["products"] == [{"id": lays, "name": "Lay's"}]
    assert found["periods"] == [
        {
            "key": p.key,
            "label": p.label,
            "first_day": p.first_day.isoformat(),
            "last_day": p.last_day.isoformat(),
        }
        for p in periods(ist_date(utc_now()))
    ]


def test_the_ready_made_periods_are_the_summaries_weeks_and_months() -> None:
    found = [(p.key, p.label, p.first_day, p.last_day) for p in periods(date(2026, 11, 3))]

    assert found == [
        ("this_week", "This week", date(2026, 11, 2), date(2026, 11, 8)),
        ("last_week", "Last week", date(2026, 10, 26), date(2026, 11, 1)),
        ("this_month", "This month", date(2026, 11, 1), date(2026, 11, 30)),
        ("last_month", "Last month", date(2026, 10, 1), date(2026, 10, 31)),
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

    assert mine.json()["issue"]["owner_name"] == "Demo Manager"
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
