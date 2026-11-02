"""The review's API (batch 14): GET /api/visits/{id}/review, PATCH and DELETE /api/insights/{id},
and POST /api/visits/{id}/insights, /notes and /confirm. The demo rep's org, on the PepsiCo
example pack (payments: finance, and the area manager when high)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from countertalk import catalog, extraction, limits
from countertalk.db import session_scope
from countertalk.models import Insight, Role, Shop, User, new_id
from countertalk.playbooks import load_org_pack
from scripts.seed_dev import DEMO_PACK, DEMO_REP, seed
from tests.conftest import a_user, sign_in
from tests.test_extraction import answer, insight, transcribed
from tests.test_quotes import PARAPHRASE
from tests.test_roles import ALL_PLACED, FakeLlm
from tests.test_show_quotes import BUSY
from tests.test_visits import Org_, make_org

PAYMENT = insight(category="payments", severity="low", suggested_team="finance")


def seeded_demo() -> Org_:
    """The demo org, rep and shops, seeded."""
    with session_scope() as session:
        seed(session, load_org_pack(*DEMO_PACK))
        rep = demo_rep_of(session)
        shops = select(Shop.id).where(Shop.org_id == rep.org_id).order_by(Shop.name)
        return Org_(rep.org_id, rep.id, session.scalars(shops).first())


def demo_rep_of(session: Session) -> User:
    return session.scalars(select(User).where(User.email == DEMO_REP["email"])).one()


def signed_in_demo(client: TestClient) -> Org_:
    """The demo org, seeded, with the client signed in as its rep."""
    found = seeded_demo()
    sign_in(client, found.rep_id)
    return found


def signed_in_manager(client: TestClient) -> Org_:
    """The demo org, seeded, with the client signed in as its manager, who sees the whole
    org (batch 16); its rep confirms (signed_in_as)."""
    found = seeded_demo()
    sign_in(client, a_user(found.org_id, Role.MANAGER, "Demo Manager"))
    return found


@pytest.fixture(autouse=True)
def demo(client: TestClient) -> Org_:
    return signed_in_demo(client)


def in_review(org: Org_, *found: dict[str, Any], busy: bool = False) -> str:
    visit_id = transcribed(org)
    extraction.extract(
        visit_id, FakeLlm(ALL_PLACED, answer(*(found or (PAYMENT,))), *((BUSY,) if busy else ()))
    )
    return visit_id


def the_review(client: TestClient, visit_id: str) -> dict[str, Any]:
    response = client.get(f"/api/visits/{visit_id}/review")
    assert response.status_code == 200, response.text
    return response.json()


def error_of(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def test_the_review_is_everything_the_page_shows(client: TestClient, demo: Org_) -> None:
    visit_id = in_review(demo)

    found = the_review(client, visit_id)

    assert (found["visit"]["id"], found["visit"]["status"]) == (visit_id, "review")
    assert found["report"] == {"summary_en": "A routine order.", "next_visit_date": "2026-11-22"}
    assert [(t["type"], t["due"], t["status"]) for t in found["tasks"]] == [
        ("callback", "2026-11-17", "open")
    ]
    [shown] = found["insights"]
    assert {k: shown[k] for k in ("kind", "category", "severity", "verified", "ai_team")} == {
        "kind": "product_request",
        "category": "payments",
        "severity": "low",
        "verified": True,
        "ai_team": "finance",
    }
    assert shown["quotes"] == [
        {
            "seq": 0,
            "turn_id": 1,
            "speaker_role": "shopkeeper",
            "text": "Kurkure ke do dabbe chahiye",  # as matched: the full stop isn't a word
            "match": "exact",
            "start_ms": 2_500,
        }
    ]
    assert [(r["slot"], r["team_key"], r["routed_by"]) for r in shown["routes"]] == [
        (1, "finance", "table")
    ]
    assert found["notes"] == []
    assert found["speaker_roles"] == {"c0:A": "rep", "c0:B": "shopkeeper", "c0:C": "other"}
    assert {"key": "payments", "label": "Credit and payments"} in found["categories"]
    assert [c["key"] for c in found["severities"]] == ["high", "medium", "low"]
    assert {"key": "product_request", "label": "Product request"} in found["kinds"]
    assert found["order_items"] == []


def test_the_review_shows_the_order_as_said_with_its_products(
    client: TestClient, demo: Org_
) -> None:
    visit_id = transcribed(demo)
    order = [
        {"name": "कुरकुरे मंच", "quantity": 2, "unit": "dabbe"},
        {"name": "Zingo", "quantity": 1.5, "unit": None},
    ]
    extraction.extract(visit_id, FakeLlm(ALL_PLACED, answer(PAYMENT, order_items=order)))

    found = the_review(client, visit_id)

    munch = "Kurkure Masala Munch"
    assert found["order_items"] == [
        {"seq": 0, "name": "कुरकुरे मंच", "quantity": 2, "unit": "dabbe", "sku": munch},
        {"seq": 1, "name": "Zingo", "quantity": 1.5, "unit": None, "sku": None},
    ]


def test_a_category_or_severity_change_re_routes_and_comes_back(
    client: TestClient, demo: Org_
) -> None:
    [shown] = the_review(client, in_review(demo))["insights"]

    response = client.patch(f"/api/insights/{shown['id']}", json={"severity": "high"})

    assert response.status_code == 200, response.text
    assert [(r["team_key"], r["routed_by"]) for r in response.json()["routes"]] == [
        ("finance", "table"),
        ("area_manager", "override"),
    ]


def test_an_unverified_insight_shows_greyed_and_can_t_be_edited(
    client: TestClient, demo: Org_
) -> None:
    unfound = insight(evidence=[{"turn_id": 3, "quote": PARAPHRASE}])
    [shown] = the_review(client, in_review(demo, unfound, busy=True))["insights"]

    assert (shown["verified"], shown["routes"], shown["quotes"][0]["start_ms"]) == (
        False,
        [],
        None,
    )
    response = client.patch(f"/api/insights/{shown['id']}", json={"summary_en": "x"})
    assert error_of(response) == (409, "insight_unverified")
    assert client.delete(f"/api/insights/{shown['id']}").status_code == 204


def test_a_deleted_insight_is_gone_from_the_review(client: TestClient, demo: Org_) -> None:
    visit_id = in_review(demo)
    [shown] = the_review(client, visit_id)["insights"]

    assert client.delete(f"/api/insights/{shown['id']}").status_code == 204

    assert the_review(client, visit_id)["insights"] == []
    assert error_of(client.delete(f"/api/insights/{shown['id']}")) == (404, "insight_not_found")


def test_an_added_finding_quotes_its_turn_and_is_routed(client: TestClient, demo: Org_) -> None:
    visit_id = in_review(demo)
    body = {
        "id": new_id(),
        "turn_id": 1,
        "kind": "problem",
        "category": "payments",
        "severity": "high",
        "summary_en": "A payout is late.",
    }

    response = client.post(f"/api/visits/{visit_id}/insights", json=body)

    assert response.status_code == 201, response.text
    added = response.json()
    assert (added["added_by_rep"], added["verified"], added["quotes"][0]["match"]) == (
        True,
        True,
        "exact",
    )
    assert [r["team_key"] for r in added["routes"]] == ["finance", "area_manager"]
    assert len(the_review(client, visit_id)["insights"]) == 2


def test_an_added_finding_quoting_the_rep_s_line_is_refused(client: TestClient, demo: Org_) -> None:
    visit_id = in_review(demo)
    body = {
        "id": new_id(),
        "turn_id": 0,  # the rep's "Namaste Asha ji, aaj kya order hai?"
        "kind": "problem",
        "category": "payments",
        "severity": "high",
        "summary_en": "A payout is late.",
    }

    response = client.post(f"/api/visits/{visit_id}/insights", json=body)

    assert error_of(response) == (400, "not_shopkeeper_turn")
    assert len(the_review(client, visit_id)["insights"]) == 1


def test_a_note_is_added_for_the_record(client: TestClient, demo: Org_) -> None:
    visit_id = in_review(demo)

    response = client.post(
        f"/api/visits/{visit_id}/notes", json={"id": new_id(), "text": "Owner away."}
    )

    assert response.status_code == 201, response.text
    assert [n["text"] for n in the_review(client, visit_id)["notes"]] == ["Owner away."]


@pytest.mark.parametrize(
    ("call", "status", "code"),
    [
        (
            lambda c, v, i: c.patch(f"/api/insights/{i}", json={"colour": "red"}),
            422,
            "invalid_request",
        ),
        (
            lambda c, v, i: c.patch(f"/api/insights/{i}", json={"category": "x"}),
            400,
            "invalid_category",
        ),
        (
            lambda c, v, i: c.patch(f"/api/insights/{i}", json={"summary_en": " "}),
            400,
            "summary_empty",
        ),
        (
            lambda c, v, i: c.post(
                f"/api/visits/{v}/insights",
                json={
                    "id": new_id(),
                    "turn_id": 99,
                    "kind": "problem",
                    "category": "payments",
                    "severity": "low",
                    "summary_en": "x",
                },
            ),
            404,
            "turn_not_found",
        ),
        (
            lambda c, v, i: c.post(f"/api/visits/{v}/insights", json={"turn_id": "1"}),
            422,
            "invalid_request",
        ),
        (
            lambda c, v, i: c.post(f"/api/visits/{v}/notes", json={"id": new_id(), "text": "  "}),
            400,
            "note_empty",
        ),
        (lambda c, v, i: c.post(f"/api/visits/{v}/notes", json={}), 422, "invalid_request"),
    ],
    ids=[
        "unknown-field",
        "unknown-category",
        "empty-summary",
        "turn-not-in-transcript",
        "turn-not-a-number",
        "empty-note",
        "no-note",
    ],
)
def test_bad_requests(client: TestClient, demo: Org_, call: Any, status: int, code: str) -> None:
    visit_id = in_review(demo)
    [shown] = the_review(client, visit_id)["insights"]

    assert error_of(call(client, visit_id, shown["id"])) == (status, code)


def test_confirm_locks_the_visit_and_confirming_again_returns_it(
    client: TestClient, demo: Org_
) -> None:
    visit_id = in_review(demo)
    [shown] = the_review(client, visit_id)["insights"]

    first = client.post(f"/api/visits/{visit_id}/confirm")
    again = client.post(f"/api/visits/{visit_id}/confirm")

    assert (first.status_code, first.json()["status"]) == (200, "confirmed")
    assert again.json() == first.json()
    assert the_review(client, visit_id)["insights"][0]["visible"] is True
    edits = [
        client.patch(f"/api/insights/{shown['id']}", json={"summary_en": "x"}),
        client.delete(f"/api/insights/{shown['id']}"),
        client.post(f"/api/visits/{visit_id}/notes", json={"id": new_id(), "text": "x"}),
    ]
    assert [error_of(response) for response in edits] == [(409, "visit_confirmed")] * 3


def test_another_org_s_visit_and_insight_are_404(client: TestClient) -> None:
    other = make_org("Another Org")
    with session_scope() as session:
        from countertalk import catalog

        catalog.import_org_pack(session, other.org_id, load_org_pack(*DEMO_PACK))
    theirs = in_review(other)
    with session_scope(read_only=True) as session:
        from countertalk.models import Insight

        insight_id = session.scalar(select(Insight.id).where(Insight.visit_id == theirs))

    assert error_of(client.get(f"/api/visits/{theirs}/review")) == (404, "visit_not_found")
    assert error_of(client.post(f"/api/visits/{theirs}/confirm")) == (404, "visit_not_found")
    patched = client.patch(f"/api/insights/{insight_id}", json={"summary_en": "x"})
    assert error_of(patched) == (404, "insight_not_found")


def test_edits_of_a_visit_still_processing_are_409(client: TestClient, demo: Org_) -> None:
    processing = transcribed(demo)

    added = client.post(
        f"/api/visits/{processing}/insights",
        json={
            "id": new_id(),
            "turn_id": 1,
            "kind": "problem",
            "category": "payments",
            "severity": "low",
            "summary_en": "x",
        },
    )

    assert error_of(added) == (409, "visit_not_in_review")
    assert error_of(
        client.post(f"/api/visits/{processing}/notes", json={"id": new_id(), "text": "x"})
    ) == (
        409,
        "visit_not_in_review",
    )
    assert error_of(client.post(f"/api/visits/{processing}/confirm")) == (
        409,
        "visit_not_in_review",
    )


def test_a_note_too_long_is_refused(client: TestClient, demo: Org_) -> None:
    visit_id = in_review(demo)
    body = {"id": new_id(), "text": "x" * (limits.NOTE_MAX_CHARS + 1)}

    assert error_of(client.post(f"/api/visits/{visit_id}/notes", json=body)) == (
        400,
        "note_too_long",
    )


def test_no_edit_reaches_another_org_s_visit(client: TestClient) -> None:
    other = make_org("Another Org")
    with session_scope() as session:
        catalog.import_org_pack(session, other.org_id, load_org_pack(*DEMO_PACK))
    theirs = in_review(other)
    with session_scope(read_only=True) as session:
        insight_id = session.scalar(select(Insight.id).where(Insight.visit_id == theirs))
    body = {
        "id": new_id(),
        "turn_id": 1,
        "kind": "problem",
        "category": "payments",
        "severity": "low",
    }

    calls = [
        client.delete(f"/api/insights/{insight_id}"),
        client.post(f"/api/visits/{theirs}/notes", json={"id": new_id(), "text": "x"}),
        client.post(f"/api/visits/{theirs}/insights", json=body | {"summary_en": "x"}),
    ]

    assert [error_of(response) for response in calls] == [
        (404, "insight_not_found"),
        (404, "visit_not_found"),
        (404, "visit_not_found"),
    ]
    with session_scope(read_only=True) as session:
        assert session.get(Insight, insight_id) is not None
