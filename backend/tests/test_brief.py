"""The pre-visit brief (batch F5): Muse Spark (a fake here) writes the words from the facts code
picks, only when asked, and each one is saved. The tiny org, signed in as its rep."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from countertalk import brief, extraction
from countertalk.api.briefs import brief_llm
from countertalk.db import session_scope
from countertalk.errors import RetryableError
from countertalk.llm.base import Outcome
from countertalk.models import Brief, LlmCall, LlmPurpose, Role
from countertalk.playbooks import PLAYBOOKS_DIR
from scripts import write_brief
from tests.conftest import ORG_PACK_FIXTURES, a_user, sign_in
from tests.test_ask import fake_answers
from tests.test_fixed import another_shop, raised
from tests.test_review_api import error_of
from tests.test_roles import FakeLlm, call, tiny_org
from tests.test_visits import Org_

WORDS = {
    "overview": ["The shop raises payments most.", "It has 2 confirmed visits."],
    "worth_raising": ["Follow up on the payment still open."],
}


@pytest.fixture
def org(client: TestClient) -> Org_:
    """An org on the tiny pack, its rep signed in: the briefs are for its one shop."""
    found = tiny_org("Brief Test Org")
    sign_in(client, found.rep_id)
    return found


@pytest.fixture(autouse=True)
def tiny_pack(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tiny org's pack, from the tests' own packs."""
    real = extraction.pack_for

    def pack_for(
        org_id: str, packs: Any = ORG_PACK_FIXTURES, books: Any = PLAYBOOKS_DIR, **given: Any
    ) -> Any:
        return real(org_id, packs, books, **given)

    monkeypatch.setattr(extraction, "pack_for", pack_for)


@pytest.fixture
def llm(app: FastAPI) -> Iterator[list[FakeLlm]]:
    """Each test's fake, put in place by fake_answers()."""
    used: list[FakeLlm] = []
    app.dependency_overrides[brief_llm] = lambda: used[-1] if used else None
    yield used
    app.dependency_overrides.clear()


def visits(org: Org_, *days_ago: float) -> list[str]:
    """Confirmed visits at the org's shop, days ago, each raising a problem."""
    return [raised(org, days, kind="problem") for days in days_ago]


def of_shop(client: TestClient, shop_id: str) -> dict[str, Any]:
    response = client.get(f"/api/shops/{shop_id}/brief")
    assert response.status_code == 200, response.text
    return response.json()


def generated(client: TestClient, shop_id: str) -> dict[str, Any]:
    response = client.post(f"/api/shops/{shop_id}/brief")
    assert response.status_code == 200, response.text
    return response.json()


def saved() -> int:
    with session_scope(read_only=True) as session:
        return session.scalar(select(func.count()).select_from(Brief)) or 0


def test_a_shop_needs_2_confirmed_visits(client: TestClient, org: Org_, llm: list[FakeLlm]) -> None:
    visits(org, 3)
    fake = fake_answers(llm, WORDS)

    found = of_shop(client, org.shop_id)
    refused = client.post(f"/api/shops/{org.shop_id}/brief")

    assert (found["state"], found["visits"], found["can_generate"], found["brief"]) == (
        "not_enough_visits",
        1,
        False,
        None,
    )
    assert error_of(refused) == (409, "brief_not_enough_visits")
    assert fake.asked == []


def test_generate_saves_a_brief_from_the_facts_only_and_logs_its_call(
    client: TestClient, org: Org_, llm: list[FakeLlm]
) -> None:
    visits(org, 5, 2)
    fake = fake_answers(llm, WORDS)

    found = generated(client, org.shop_id)

    shown = found["brief"]
    assert (found["state"], found["since"], found["can_generate"]) == ("up_to_date", 0, False)
    assert (shown["after_visit"], shown["overview"], shown["worth_raising"]) == (
        2,
        WORDS["overview"],
        WORDS["worth_raising"],
    )
    assert [item["category"] for item in shown["last_visit"]["raised"]] != []
    assert set(shown["last_visit"]["raised"][0]) == {"category", "problem", "quote"}
    assert [row["status"] for row in shown["still_open"]] == ["open"]  # both visits' one issue
    assert of_shop(client, org.shop_id) == found  # read again: no call
    assert len(fake.asked) == 1
    sent = json.loads(fake.asked[0][0][1].content)
    assert sent["confirmed_visits"] == 2
    assert "transcript" not in json.dumps(sent) and "Rep" not in json.dumps(sent)
    with session_scope(read_only=True) as session:
        logged = session.scalars(select(LlmCall).where(LlmCall.purpose == LlmPurpose.BRIEF)).one()
        row = session.scalars(select(Brief)).one()
        assert (logged.purpose, logged.brief_id) == (LlmPurpose.BRIEF, shown["id"])
        assert (row.user_id, row.visits, row.model) == (org.rep_id, 2, "fake-spark-1")


def test_regenerate_only_after_a_newer_confirmed_visit_and_older_ones_are_kept(
    client: TestClient, org: Org_, llm: list[FakeLlm]
) -> None:
    visits(org, 5, 2)
    fake_answers(llm, WORDS, WORDS)
    first = generated(client, org.shop_id)["brief"]

    again = client.post(f"/api/shops/{org.shop_id}/brief")
    visits(org, 1)
    behind = of_shop(client, org.shop_id)
    latest = generated(client, org.shop_id)

    assert error_of(again) == (409, "brief_up_to_date")
    assert (behind["state"], behind["since"], behind["can_generate"]) == ("out_of_date", 1, True)
    assert behind["brief"]["id"] == first["id"]
    assert (latest["state"], latest["brief"]["after_visit"]) == ("up_to_date", 3)
    assert latest["brief"]["id"] != first["id"]
    assert saved() == 2


def test_the_checker_drops_a_number_or_a_name_not_in_the_facts() -> None:
    facts = {"confirmed_visits": 3, "shop": {"name": "Asha Stores"}, "still_open": [{"a": "x"}]}
    names = ["priya sharma", "priya", "asha"]
    answer = {
        "overview": [
            "It has 3 confirmed visits.",
            "It owes ₹4,500.",  # not in the facts
            "Priya visited last.",  # one of the org's users
            "Asha Stores has one problem still open.",  # the shop's own name; 1 is a list's length
            "Seven more.",
            "It is in good shape.",
            "Things are calm.",
            "One too many.",  # past the room
        ],
        "worth_raising": ["  ", "Ask about the 2 schemes."],
    }

    overview, raising, dropped = brief.checked(answer, facts, names)

    assert overview == [
        "It has 3 confirmed visits.",
        "Asha Stores has one problem still open.",
        "It is in good shape.",
        "Things are calm.",
    ]
    assert raising == []
    assert dropped == {"extra": 1, "number": 3, "name": 1}


def test_invented_numbers_never_reach_the_saved_brief(
    client: TestClient, org: Org_, llm: list[FakeLlm]
) -> None:
    visits(org, 5, 2)
    fake_answers(llm, {"overview": ["It ordered 40 cases."], "worth_raising": []})

    shown = generated(client, org.shop_id)["brief"]

    assert (shown["overview"], shown["worth_raising"]) == ([], [])
    with session_scope(read_only=True) as session:
        assert session.scalars(select(Brief.content)).one()["dropped"]["number"] == 1


def test_a_failed_call_saves_nothing_and_without_a_key_nothing_is_sent(
    client: TestClient, org: Org_, llm: list[FakeLlm]
) -> None:
    visits(org, 5, 2)
    without = client.post(f"/api/shops/{org.shop_id}/brief")
    failing = Outcome(calls=(call("timeout"),), error=RetryableError("llm_timeout", "Too slow"))
    fake_answers(llm, failing)

    failed = client.post(f"/api/shops/{org.shop_id}/brief")

    assert error_of(without) == (503, "llm_not_configured")
    assert error_of(failed) == (503, "llm_timeout")
    assert saved() == 0
    with session_scope(read_only=True) as session:
        logged = session.scalars(select(LlmCall).where(LlmCall.purpose == LlmPurpose.BRIEF)).one()
        assert (logged.purpose, logged.brief_id, logged.ok) == (LlmPurpose.BRIEF, None, False)


def test_the_briefs_page_lists_every_shop_with_its_state(
    client: TestClient, org: Org_, llm: list[FakeLlm]
) -> None:
    visits(org, 5, 2)
    fake_answers(llm, WORDS)
    generated(client, org.shop_id)
    ready = another_shop(org, "Zeta Store")
    visits(ready, 4, 3)
    few = another_shop(org, "Alpha Store")
    visits(few, 4)

    response = client.get("/api/briefs")

    assert response.status_code == 200, response.text
    found = [(row["shop_id"], row["state"], row["since"]) for row in response.json()["items"]]
    assert found[:3] == [
        (org.shop_id, "up_to_date", 0),
        (ready.shop_id, "none", 0),
        (few.shop_id, "not_enough_visits", 0),
    ]
    assert response.json()["items"][0]["generated_at"] is not None


def test_a_team_member_and_another_org_get_404(
    client: TestClient, org: Org_, llm: list[FakeLlm]
) -> None:
    other = tiny_org("Other Org")
    mine = client.get(f"/api/shops/{other.shop_id}/brief")
    sign_in(client, a_user(org.org_id, Role.TEAM_MEMBER, "Money Member", "money"))

    assert error_of(mine) == (404, "shop_not_found")
    for response in (
        client.get("/api/briefs"),
        client.get(f"/api/shops/{org.shop_id}/brief"),
        client.post(f"/api/shops/{org.shop_id}/brief"),
    ):
        assert error_of(response) == (404, "not_found")


def test_the_paid_check_s_dry_run_sends_nothing_and_yes_writes_each_brief(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visits(org, 5, 2)
    a_user(org.org_id, Role.MANAGER, "Brief Manager")
    fake = FakeLlm(WORDS)

    dry = write_brief.main([org.org_id])
    printed = capsys.readouterr().out
    wrote = write_brief.main([org.org_id, "--yes"], fake)

    assert (dry, saved(), wrote) == (0, 1, 0)
    assert "1 briefs" in printed and "Nothing sent" in printed
    assert len(fake.asked) == 1
    assert "cost: $" in capsys.readouterr().out


def test_a_call_paid_for_is_counted_even_when_its_brief_couldn_t_be_saved(
    client: TestClient, org: Org_, llm: list[FakeLlm], monkeypatch: pytest.MonkeyPatch
) -> None:
    visits(org, 5, 2)
    fake_answers(llm, WORDS)

    def no_save(*_: Any) -> str:
        raise RetryableError("database_unavailable", "The database went away")

    monkeypatch.setattr(brief, "_save", no_save)

    assert error_of(client.post(f"/api/shops/{org.shop_id}/brief"))[0] == 503
    assert saved() == 0
    with session_scope(read_only=True) as session:
        logged = session.scalars(select(LlmCall).where(LlmCall.purpose == LlmPurpose.BRIEF)).one()
        assert (logged.ok, logged.brief_id) == (True, None)
