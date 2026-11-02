"""Merge suggestions in the inbox (batch 15c): an issue shows its open suggestions, Merge merges
as suggested (and again changes nothing), Dismiss keeps the pair apart for good. The demo org:
two low payments at shops of two distributors are two of finance's issues."""

import pytest
from fastapi.testclient import TestClient

from countertalk import issues
from countertalk.db import session_scope
from countertalk.models import Issue, IssueMergeSuggestion
from tests.test_inbox_api import confirmed, inbox, issue, shop
from tests.test_merge import two_issues
from tests.test_review_api import error_of, signed_in_manager
from tests.test_visits import Org_


@pytest.fixture(autouse=True)
def demo(client: TestClient) -> Org_:
    return signed_in_manager(client)


@pytest.fixture
def suggested(client: TestClient, demo: Org_) -> tuple[str, str, str]:
    """Finance's two payment issues, d3's and d2's, and a suggestion to keep d3's: the
    suggestion's, the kept issue's and the other's IDs."""
    confirmed(client, shop(demo, "Corner Daily Needs"))  # d3
    confirmed(client, shop(demo, "Lotus General Store"))  # d2
    cards = {c["title_en"].split(" · ")[1]: c for c in inbox(client, "finance")}
    kept = cards["Station Road Distributors (example)"]["id"]
    other = next(
        c["id"] for name, c in cards.items() if name != "Station Road Distributors (example)"
    )
    with session_scope() as session:
        found = IssueMergeSuggestion(
            org_id=demo.org_id,
            team_id=team_of(kept),
            into_issue_id=kept,
            merge_issue_id=other,
            pair_key=issues.pair_key(kept, other),
            reason="Both are the same payment dispute.",
        )
        session.add(found)
        session.flush()
        return found.id, kept, other


def team_of(issue_id: str) -> str:
    with session_scope(read_only=True) as session:
        return session.get_one(Issue, issue_id).team_id


def merge(client: TestClient, suggestion_id: str, kept: str, other: str) -> object:
    body = {"into_version": issue(client, kept)["issue"]["version"]}
    body["merge_version"] = issue(client, other)["issue"]["version"]
    return client.post(f"/api/issues/suggestions/{suggestion_id}/merge", json=body)


def test_each_of_its_issues_shows_the_suggestion_and_the_other_one(
    client: TestClient, suggested: tuple[str, str, str]
) -> None:
    suggestion_id, kept, other = suggested

    seen_from_kept = issue(client, kept)["suggestions"]
    seen_from_other = issue(client, other)["suggestions"]

    assert [(s["id"], s["into_issue_id"], s["merge_issue_id"]) for s in seen_from_kept] == [
        (suggestion_id, kept, other)
    ]
    assert seen_from_kept[0]["reason"] == "Both are the same payment dispute."
    assert (seen_from_kept[0]["other"]["id"], seen_from_other[0]["other"]["id"]) == (other, kept)
    assert seen_from_other[0]["other"]["version"] == issue(client, kept)["issue"]["version"]


def test_merge_merges_as_suggested_and_again_changes_nothing(
    client: TestClient, suggested: tuple[str, str, str]
) -> None:
    suggestion_id, kept, other = suggested
    body = {
        "into_version": issue(client, kept)["issue"]["version"],
        "merge_version": issue(client, other)["issue"]["version"],
    }

    first = client.post(f"/api/issues/suggestions/{suggestion_id}/merge", json=body)
    again = client.post(f"/api/issues/suggestions/{suggestion_id}/merge", json=body)

    assert first.status_code == again.status_code == 200, first.text
    found = first.json()
    assert (found["issue"]["id"], found["issue"]["shops"], found["suggestions"]) == (kept, 2, [])
    assert again.json() == found
    assert [c["id"] for c in inbox(client, "finance")] == [kept]
    dismissed = client.post(f"/api/issues/suggestions/{suggestion_id}/dismiss")
    assert error_of(dismissed) == (409, "suggestion_merged")


def test_dismiss_keeps_them_apart_and_merge_then_is_409(
    client: TestClient, suggested: tuple[str, str, str]
) -> None:
    suggestion_id, kept, other = suggested

    response = client.post(f"/api/issues/suggestions/{suggestion_id}/dismiss")

    assert response.status_code == 204, response.text
    assert issue(client, kept)["suggestions"] == issue(client, other)["suggestions"] == []
    assert error_of(merge(client, suggestion_id, kept, other)) == (409, "suggestion_dismissed")
    assert len(inbox(client, "finance")) == 2


def test_merge_on_an_old_read_is_409_and_merges_nothing(
    client: TestClient, suggested: tuple[str, str, str]
) -> None:
    suggestion_id, kept, other = suggested
    body = {"into_version": issue(client, kept)["issue"]["version"], "merge_version": 0}

    response = client.post(f"/api/issues/suggestions/{suggestion_id}/merge", json=body)

    assert error_of(response) == (409, "issue_changed")
    assert len(inbox(client, "finance")) == 2


def test_another_org_s_suggestion_is_404(client: TestClient) -> None:
    theirs, low, high, _ = two_issues()
    with session_scope() as session:
        found = IssueMergeSuggestion(
            org_id=theirs.org_id,
            team_id=team_of(low),
            into_issue_id=low,
            merge_issue_id=high,
            pair_key=issues.pair_key(low, high),
            reason="Theirs.",
        )
        session.add(found)
        session.flush()
        suggestion_id = found.id
    body = {"into_version": 1, "merge_version": 1}

    merged = client.post(f"/api/issues/suggestions/{suggestion_id}/merge", json=body)
    dismissed = client.post(f"/api/issues/suggestions/{suggestion_id}/dismiss")

    assert error_of(merged) == error_of(dismissed) == (404, "suggestion_not_found")
