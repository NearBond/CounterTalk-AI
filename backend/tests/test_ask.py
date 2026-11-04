"""Ask a question across visits (batch 17d): Muse Spark (a fake here) turns the question into the
inbox's filter, and code checks it and runs it. The demo org, on the PepsiCo India example pack:
payments go to finance, and to the area manager when high."""

import json
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from countertalk import ask, limits
from countertalk.api import ask as api_ask
from countertalk.api.ask import ask_llm
from countertalk.db import get_engine, session_scope
from countertalk.errors import PermanentError, RetryableError
from countertalk.llm.base import Outcome
from countertalk.models import Ask, LlmCall, Role, ist_date, utc_now
from countertalk.summaries import at_midnight
from tests.conftest import a_user, sign_in
from tests.test_extraction import insight
from tests.test_inbox_api import HIGH, UNLINKED, change_visit, confirmed, item, shop
from tests.test_review_api import error_of, signed_in_manager
from tests.test_roles import FakeLlm, call, tiny_org
from tests.test_summaries import visit_at
from tests.test_visits import Org_

QUESTION = "Lay's ki payment ki shikayat, Old Market, is mahine"


def answer(**fields: Any) -> dict[str, Any]:
    """Muse Spark's answer: understood, nothing named, any status, unless said."""
    found = {
        "category": None,
        "competitor": None,
        "product": None,
        "area": None,
        "period": None,
        "status": "any",
        "understood": True,
        "reason": None,
    }
    return found | fields


@pytest.fixture
def demo(client: TestClient) -> Org_:
    return signed_in_manager(client)


@pytest.fixture
def llm(app: FastAPI) -> Iterator[list[FakeLlm]]:
    """Each test's fake, put in place by fake_answers()."""
    used: list[FakeLlm] = []
    app.dependency_overrides[ask_llm] = lambda: used[-1]
    yield used
    app.dependency_overrides.clear()


def fake_answers(llm: list[FakeLlm], *answers: dict[str, Any] | Outcome) -> FakeLlm:
    llm.append(FakeLlm(*answers))
    return llm[-1]


def asked(client: TestClient, question: str = QUESTION) -> dict[str, Any]:
    response = client.post("/api/ask", json={"question": question})
    assert response.status_code == 200, response.text
    return response.json()


def asks() -> int:
    with session_scope(read_only=True) as session:
        return session.scalar(select(func.count()).select_from(Ask)) or 0


def test_a_question_becomes_chips_and_the_findings_that_meet_them(
    client: TestClient, demo: Org_, llm: list[FakeLlm]
) -> None:
    sunrise = confirmed(client, shop(demo, "Sunrise Kirana"), UNLINKED)  # Old Market
    corner = confirmed(client, shop(demo, "Corner Daily Needs"), UNLINKED)  # Station Road
    confirmed(client, shop(demo, "Lotus General Store"), UNLINKED)  # Old Market, unlinked
    masala = item(demo, "lays_magic_masala")
    change_visit(sunrise, sku_id=masala)  # Lay's takes in its products
    change_visit(corner, sku_id=masala)
    fake = fake_answers(
        llm,
        answer(category="payments", product="lays", area="Old Market", period="this_month"),
    )

    found = asked(client)

    assert found["understood"] is True
    assert found["reason"] is None
    assert [(chip["name"], chip["value"]) for chip in found["chips"]] == [
        ("Category", "Credit and payments"),
        ("Product", "Lay's"),
        ("Area", "Old Market"),
        ("Period", "This month"),
        ("Status", "Any status"),
    ]
    [finding] = found["findings"]
    assert (finding["visit_id"], finding["shop_name"], finding["area"]) == (
        sunrise,
        "Sunrise Kirana",
        "Old Market",
    )
    assert finding["insight"]["quotes"][0]["text"] == "Kurkure ke do dabbe chahiye"
    # One call: the question as text in the prompt, the org's keys in the schema.
    [(messages, name, schema)] = fake.asked
    sent = json.loads(messages[1].content)
    assert (name, sent["question"]) == ("inbox_filter", QUESTION)
    assert {"key": "lays", "name": "Lay's"} in sent["products"]
    assert "Old Market" in schema["properties"]["area"]["enum"]
    with session_scope(read_only=True) as session:
        logged = session.scalars(select(LlmCall.purpose).where(LlmCall.visit_id.is_(None))).all()
    assert (logged, asks()) == (["ask"], 1)


def test_the_newest_findings_come_first_at_most_the_limit(
    client: TestClient, demo: Org_, llm: list[FakeLlm], monkeypatch: pytest.MonkeyPatch
) -> None:
    older = confirmed(client, shop(demo, "Sunrise Kirana"), UNLINKED)
    change_visit(older, utc_now() - timedelta(days=2))
    newer = confirmed(client, shop(demo, "Lotus General Store"), UNLINKED)
    monkeypatch.setattr(limits, "ASK_FINDINGS_MAX", 1)
    fake_answers(llm, answer(), answer())

    assert [f["visit_id"] for f in asked(client)["findings"]] == [newer]
    monkeypatch.setattr(limits, "ASK_FINDINGS_MAX", 2)
    assert [f["visit_id"] for f in asked(client)["findings"]] == [newer, older]


def test_a_key_that_isn_t_the_org_s_is_refused_and_runs_no_query(
    client: TestClient, demo: Org_, llm: list[FakeLlm]
) -> None:
    confirmed(client, shop(demo, "Sunrise Kirana"), UNLINKED)
    fake_answers(llm, answer(competitor="pringles"))  # a competitor the catalog hasn't

    found = asked(client)

    assert found == {
        "understood": False,
        "reason": "It named a competitor that isn't one of the org's",
        "chips": [],
        "findings": [],
    }


def test_a_question_not_understood_says_why(
    client: TestClient, demo: Org_, llm: list[FakeLlm]
) -> None:
    fake_answers(llm, answer(understood=False, reason="It's about the weather, not the shops."))

    found = asked(client, "Kal barish hogi?")

    assert (found["understood"], found["reason"], found["findings"]) == (
        False,
        "It's about the weather, not the shops.",
        [],
    )


def test_an_answer_that_never_fits_isn_t_understood(
    client: TestClient, demo: Org_, llm: list[FakeLlm]
) -> None:
    unfit = PermanentError("llm_answer_invalid", "No.")
    fake_answers(llm, Outcome(calls=(call("llm_answer_invalid"),), error=unfit))

    found = asked(client)

    assert (found["understood"], found["reason"]) == (False, ask.NOT_UNDERSTOOD)


def test_the_question_never_reaches_sql(client: TestClient, demo: Org_, llm: list[FakeLlm]) -> None:
    question = "x'; DROP TABLE issues; -- Lay's"
    fake = fake_answers(llm, answer(category="payments"))
    sent: list[str] = []

    def seen(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        sent.append(f"{statement} {parameters!r}")

    event.listen(get_engine(), "before_cursor_execute", seen)
    try:
        asked(client, question)
    finally:
        event.remove(get_engine(), "before_cursor_execute", seen)

    assert sent != []
    assert [statement for statement in sent if "DROP TABLE" in statement] == []
    assert json.loads(fake.asked[0][0][1].content)["question"] == question


def test_another_org_s_findings_never_come_back(
    client: TestClient, demo: Org_, llm: list[FakeLlm]
) -> None:
    ours = confirmed(client, shop(demo, "Sunrise Kirana"), UNLINKED)
    theirs = tiny_org("Another Org")
    when = utc_now() - timedelta(days=1)
    visit_at(theirs, when, when, insight(category="payments", severity="low"))
    fake_answers(llm, answer(category="payments"))

    found = asked(client)

    assert [finding["visit_id"] for finding in found["findings"]] == [ours]


def test_a_team_member_s_findings_are_their_own_team_s(
    client: TestClient, demo: Org_, llm: list[FakeLlm]
) -> None:
    payments = confirmed(client, shop(demo, "Sunrise Kirana"), HIGH)  # finance and area manager
    supply = insight(category="supply", suggested_team="distribution")
    confirmed(client, shop(demo, "Corner Daily Needs"), supply)  # not finance's
    sign_in(client, a_user(demo.org_id, Role.TEAM_MEMBER, "Finance Member", "finance"))
    fake_answers(llm, answer())

    found = asked(client)

    assert found["chips"][0] == {"name": "Team", "value": "Finance"}
    [finding] = found["findings"]
    assert finding["visit_id"] == payments
    # Only their team's route, though the area manager's has it too (a visit page's rule).
    assert [route["team_key"] for route in finding["insight"]["routes"]] == ["finance"]


def test_a_user_asks_at_most_the_day_s_limit_and_a_failed_call_counts(
    client: TestClient, demo: Org_, llm: list[FakeLlm], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "ASK_DAILY_MAX", 2)
    busy = RetryableError("llm_unavailable", "Muse Spark is busy")
    fake_answers(llm, Outcome(calls=(call("llm_unavailable"),), error=busy), answer())

    failed = client.post("/api/ask", json={"question": QUESTION})
    assert error_of(failed) == (503, "llm_unavailable")
    assert asked(client)["understood"] is True
    refused = client.post("/api/ask", json={"question": QUESTION})

    assert error_of(refused) == (429, "ask_limit")
    assert refused.json()["error"]["message"] == (
        "That's 2 questions today, the most a day. Ask again tomorrow."
    )
    assert 0 < int(refused.headers["retry-after"]) <= 24 * 60 * 60
    assert asks() == 2  # the refused one isn't counted


def test_yesterday_s_questions_don_t_count_today(
    client: TestClient, demo: Org_, llm: list[FakeLlm], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "ASK_DAILY_MAX", 1)
    fake_answers(llm, answer(), answer())
    asked(client)
    with session_scope() as session:  # that one, as if asked just before today began
        row = session.scalars(select(Ask)).one()
        row.created_at = at_midnight(ist_date(utc_now())) - timedelta(seconds=1)

    assert asked(client)["understood"] is True


def test_without_a_key_asking_is_unavailable_and_not_counted(
    client: TestClient, demo: Org_
) -> None:
    response = client.post("/api/ask", json={"question": QUESTION})

    assert error_of(response) == (503, "llm_not_configured")
    assert asks() == 0


@pytest.mark.parametrize("question", ["   ", "x" * (limits.ASK_QUESTION_MAX_CHARS + 1)])
def test_a_question_is_1_to_300_characters(
    client: TestClient, demo: Org_, llm: list[FakeLlm], question: str
) -> None:
    fake_answers(llm)

    response = client.post("/api/ask", json={"question": question})

    assert error_of(response) == (422, "invalid_request")
    assert asks() == 0


def test_ask_waits_briefly_on_one_client_per_key() -> None:
    first = api_ask._client("key-one")  # noqa: SLF001

    assert api_ask._client("key-one") is first  # noqa: SLF001
    sdk = first._client  # noqa: SLF001
    assert sdk is not None
    assert (sdk.timeout, sdk.max_retries) == (
        limits.ASK_LLM_TIMEOUT_MS / 1000,
        limits.ASK_LLM_MAX_RETRIES,
    )


def test_the_last_30_days_and_the_statuses_are_the_inbox_s_filters() -> None:
    vocabulary = ask.Vocabulary({}, {}, {}, {}, {})
    today = date(2026, 11, 3)

    found = ask.understood(answer(period="last_30_days", status="open"), vocabulary, today)

    assert not isinstance(found, str)
    filters, chips = found
    assert (filters.first_day, filters.last_day) == (date(2026, 10, 5), today)
    assert filters.statuses == ("open", "in_progress")
    assert chips == [("Period", "Last 30 days"), ("Status", "Open and in progress")]
