"""A summary's narrative (batch 15b): what Muse Spark is given (the top issues, their numbers,
three quotes each, and no rep names), the checker that keeps only sentences citing the scope's
and period's shown insights with the summary's own numbers, and the job that writes it."""

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from countertalk import consent, narrative, worker
from countertalk.db import session_scope
from countertalk.errors import PermanentError, RetryableError
from countertalk.llm.base import Outcome
from countertalk.models import Insight, InsightRoute, Issue, LlmCall, Shop, Team, User
from countertalk.summaries import TeamNumbers, org_numbers, team_numbers
from tests.conftest import ORG_PACK_FIXTURES
from tests.test_extraction import insight
from tests.test_inbox_routing import only_visit_of
from tests.test_review import PACK
from tests.test_roles import FakeLlm, call, tiny_org
from tests.test_summaries import visit_at
from tests.test_summary_jobs import LAST_WEEK, WORKER_ID, asked, jobs_of, summary_row, team
from tests.test_visits import Org_


def day(n: int) -> Any:
    """Last week's day n (Monday is 0), at 11:30 IST."""
    return LAST_WEEK.start + timedelta(days=n, hours=6)


def at_shop(org: Org_, name: str, area: str = "Test Area") -> Org_:
    with session_scope() as session:
        shop = Shop(org_id=org.org_id, name=name, area=area)
        session.add(shop)
        session.flush()
        consent.mark_unanswered(session, org.org_id, org.rep_id, "hi")  # batch 17
        return Org_(org.org_id, org.rep_id, shop.id)


def paid(org: Org_, when: Any, severity: str = "low", said: str = "Payments.") -> str:
    """A payment raised on a visit at `when`, confirmed: its insight's ID."""
    found = insight(category="payments", severity=severity, suggested_team="money", summary_en=said)
    visit_id = visit_at(org, when, when, found)
    with session_scope(read_only=True) as session:
        return session.scalars(select(Insight.id).where(Insight.visit_id == visit_id)).one()


def money(org: Org_) -> TeamNumbers:
    with session_scope(read_only=True) as session:
        return team_numbers(session, PACK, org.org_id, team(org.org_id, "money"), LAST_WEEK)


def request(org: Org_) -> narrative.Request:
    with session_scope(read_only=True) as session:
        return narrative.request_for(session, PACK, org.org_id, money(org))


def checked(org: Org_, answer: dict[str, Any]) -> dict[str, Any]:
    numbers = money(org)
    with session_scope(read_only=True) as session:
        asked_for = narrative.request_for(session, PACK, org.org_id, numbers)
        return narrative.checked(session, org.org_id, numbers, asked_for, answer)


def issue_of(insight_id: str) -> str:
    """The money team's issue the insight is in."""
    with session_scope(read_only=True) as session:
        query = (
            select(InsightRoute.issue_id)
            .join(Team, Team.id == InsightRoute.team_id)
            .where(InsightRoute.insight_id == insight_id, Team.key == "money")
        )
        return session.scalars(query).one()


def said(text: str, *cites: str) -> dict[str, Any]:
    return {"text": text, "cites": list(cites)}


# --- what it's given -------------------------------------------------------------------------


def test_the_quotes_are_the_highest_severity_then_the_newest_one_per_shop_three_at_most() -> None:
    org = tiny_org()
    b, c, d = (at_shop(org, name) for name in ("Shop B", "Shop C", "Shop D"))
    paid(org, day(2), "medium", "A, medium, newer than B's")
    a_high = paid(org, day(1), "high", "A, high")
    b_medium = paid(b, day(0), "medium", "B, medium")
    c_newest = paid(c, day(3), "low", "C, low, the newest")
    paid(d, day(2), "low", "D, low")

    found = request(org)

    [issue] = json.loads(found.messages[1].content)["issues"]
    assert [q["in_english"] for q in issue["quotes"]] == [
        "A, high",
        "B, medium",
        "C, low, the newest",
    ]
    assert issue["quotes"][0]["shopkeeper_said"] == "Kurkure ke do dabbe chahiye"
    assert found.offered == (a_high, b_medium, c_newest)
    sentence = found.schema["$defs"]["sentence"]
    assert sentence["properties"]["cites"]["items"]["enum"] == list(found.offered)
    assert found.schema["properties"]["issues"]["items"]["properties"]["id"]["enum"] == [
        issue["id"]
    ]


def three_issues(org: Org_) -> tuple[str, str, str]:
    """Money's issues, ranked: one still open from the week before, with no quote this week; then
    two quoted this week, in two other areas, the newer first. Their insights' IDs."""
    older = paid(org, day(-3), "high")
    far = paid(at_shop(org, "Far Shop", area="Far Area"), day(0))
    newer = paid(at_shop(org, "New Shop", area="New Area"), day(1))
    return older, newer, far


def test_the_top_issues_with_a_quote_from_the_period_are_the_ones_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(narrative.limits, "NARRATIVE_ISSUES", 1)
    org = tiny_org()
    older, newer, far = three_issues(org)

    found = request(org)

    ranked = [issue_of(older), issue_of(newer), issue_of(far)]
    assert [n.issue_id for n in money(org).issues] == ranked
    assert [i["id"] for i in json.loads(found.messages[1].content)["issues"]] == [issue_of(newer)]
    assert found.schema["properties"]["issues"]["items"]["properties"]["id"]["enum"] == [
        issue_of(newer)
    ]
    assert found.offered == (newer,)


def test_a_cause_with_no_quote_from_the_period_isn_t_offered_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(narrative.limits, "NARRATIVE_ISSUES", 1)
    org = tiny_org()
    older, newer, _ = three_issues(org)

    with session_scope(read_only=True) as session:
        numbers = org_numbers(session, PACK, org.org_id, LAST_WEEK)
        found = narrative.request_for(session, PACK, org.org_id, numbers)

    # The older one's cause, in money's and field's issues, ranks first with no quote.
    assert numbers.causes[0].issue_key == key_of(issue_of(older))
    enum = found.schema["properties"]["issues"]["items"]["properties"]["id"]["enum"]
    assert enum == [key_of(issue_of(newer))]
    assert found.offered == (newer,)


def key_of(issue_id: str) -> str:
    with session_scope(read_only=True) as session:
        return session.get_one(Issue, issue_id).issue_key


def test_it_gets_the_top_ten_issues_and_no_rep_s_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(narrative.limits, "NARRATIVE_ISSUES", 1)
    org = tiny_org()
    with session_scope() as session:
        session.execute(update(User).where(User.id == org.rep_id).values(name="Asha Example"))
    paid(org, day(0))
    visit_at(org, day(0), day(0), insight(category="payments", severity="high"))  # also field's

    found = request(org)

    assert len(json.loads(found.messages[1].content)["issues"]) == 1
    assert "Asha" not in "".join(m.content for m in found.messages)


# --- the checker -----------------------------------------------------------------------------


def test_the_checker_keeps_only_cited_sentences_with_their_own_numbers() -> None:
    org = tiny_org()
    inside = paid(org, day(0))
    earlier = paid(org, day(-3))  # the week before
    hidden = paid(org, day(1))
    with session_scope() as session:
        session.execute(update(Insight).where(Insight.id == hidden).values(visible=False))
    supply = visit_at(org, day(1), day(1), insight(category="supply", suggested_team="field"))
    with session_scope(read_only=True) as session:
        elsewhere = session.scalars(select(Insight.id).where(Insight.visit_id == supply)).one()
    far = paid(at_shop(org, "Far Shop", area="Sector 51"), day(2))  # another area, issue
    issue, far_issue = issue_of(inside), issue_of(far)

    found = checked(
        org,
        {
            "overview": [
                said("2 shops raised their payments.", inside, far),
                said("Nothing backs this.", *[]),
                said("Seventeen shops complained.", inside),
                said("A shop wants Rs 4,500 back.", inside),
            ],
            "issues": [
                {
                    "id": issue,
                    "sentences": [
                        said("An earlier visit said so.", inside, earlier),
                        said("The field team heard it.", elsewhere),
                        said("A hidden finding.", hidden),
                        said("Made up.", str(uuid.uuid4())),
                        said("The far shop said it too.", far),
                        said("0 weeks old.", inside),  # the far issue's age, not this one's
                        said("  The shop wants its payment.  ", inside, inside),
                        said("It's still open.", inside),
                        said("A third sentence.", inside),
                    ],
                },
                {"id": issue, "sentences": [said("The same issue again.", inside)]},
                {"id": far_issue, "sentences": [said("Sector 51's shop is waiting.", far)]},
            ],
        },
    )

    cites = [{"insight_id": inside, "visit_id": only_visit_of(inside)}]
    far_cites = [{"insight_id": far, "visit_id": only_visit_of(far)}]
    assert found == {
        "overview": [{"text": "2 shops raised their payments.", "cites": cites + far_cites}],
        "issues": [
            {
                "id": issue,
                "sentences": [
                    {"text": "The shop wants its payment.", "cites": cites},
                    {"text": "It's still open.", "cites": cites},
                ],
            },
            {
                "id": far_issue,
                "sentences": [{"text": "Sector 51's shop is waiting.", "cites": far_cites}],
            },
        ],
        "kept": 4,
        "dropped": {"extra": 2, "uncited": 1, "citation": 5, "number": 3},
    }


def test_a_quote_hidden_or_moved_while_the_narrative_was_written_is_no_citation() -> None:
    org = tiny_org()
    b, c = at_shop(org, "Shop B"), at_shop(org, "Shop C")
    hidden, moved, kept = paid(org, day(0)), paid(b, day(1)), paid(c, day(2))
    numbers = money(org)
    with session_scope(read_only=True) as session:
        asked_for = narrative.request_for(session, PACK, org.org_id, numbers)
    assert set(asked_for.offered) == {hidden, moved, kept}
    with session_scope() as session:
        session.execute(update(Insight).where(Insight.id == hidden).values(visible=False))
        session.execute(
            update(InsightRoute)
            .where(InsightRoute.insight_id == moved)
            .values(team_id=team(org.org_id, "field"), issue_id=None)
        )
    sentences = [said("Hidden.", hidden), said("Moved.", moved), said("Kept.", kept)]

    with session_scope(read_only=True) as session:
        found = narrative.checked(
            session, org.org_id, numbers, asked_for, {"overview": sentences, "issues": []}
        )

    assert ([s["text"] for s in found["overview"]], found["dropped"]["citation"]) == (["Kept."], 2)


def test_the_overview_keeps_its_first_three_and_none_left_is_only_the_numbers() -> None:
    org = tiny_org()
    inside = paid(org, day(0))

    four = checked(org, {"overview": [said("Fine.", inside)] * 4, "issues": []})
    none = checked(org, {"overview": [said("2 shops.", inside)], "issues": []})

    assert (four["kept"], four["dropped"]["extra"]) == (3, 1)
    assert (none["overview"], none["issues"], none["kept"]) == ([], [], 0)


def test_numbers_are_digits_and_number_words_but_not_digits_in_names() -> None:
    found = narrative.numbers_in(
        "4,500 at 0.5, then 3, twelve or Two; 7UP, 250ml and S01; thirty, a dozen, half"
    )

    assert found == [4500.0, 0.5, 3.0, 12.0, 2.0, 30.0, 12.0, 0.5]


# --- the job ---------------------------------------------------------------------------------


def write(org: Org_, *answers: dict[str, Any] | Outcome, llm: Any = FakeLlm) -> str:
    """Last week's money summary, asked for and written by a worker with an LLM giving these
    answers (or none at all, for llm=None): its ID."""
    summary_id = asked(org)
    client = None if llm is None else llm(*answers)
    handlers = {"summary": worker.summarise_with(client, packs=ORG_PACK_FIXTURES)}
    worker.Worker(WORKER_ID, handlers=handlers).run_once()
    return summary_id


def test_the_job_keeps_the_narrative_logs_none_of_its_words_and_counts_its_cost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    org = tiny_org()
    inside = paid(org, day(0), said="The shop wants its credit note.")
    sentence = "1 shop is waiting on its credit note."
    caplog.clear()

    with caplog.at_level(logging.DEBUG):
        summary_id = write(org, {"overview": [said(sentence, inside)], "issues": []})

    found = summary_row(summary_id)
    assert (found.status, found.model, found.narrative and found.narrative["kept"]) == (
        "ready",
        "fake-spark-1",
        1,
    )
    for words in (sentence, "credit note", "Kurkure"):
        assert words not in caplog.text
    with session_scope(read_only=True) as session:
        [cost] = session.scalars(select(LlmCall).where(LlmCall.summary_id == summary_id))
        assert (cost.purpose, cost.visit_id, cost.input_tokens) == ("summary", None, 900)


def test_an_answer_that_doesn_t_fit_fails_the_summary() -> None:
    org = tiny_org()
    paid(org, day(0))
    invalid = PermanentError("llm_answer_invalid", "The answer didn't match its schema")

    summary_id = write(org, Outcome(calls=(call("llm_answer_invalid"),), error=invalid))

    found = summary_row(summary_id)
    assert (found.status, found.failure_reason) == (
        "failed",
        "Muse Spark's answer didn't fit, even after a repair",
    )


def test_a_busy_model_leaves_the_summary_to_its_next_try() -> None:
    org = tiny_org()
    paid(org, day(0))
    busy = RetryableError("llm_busy", "Muse Spark is busy")

    summary_id = write(org, Outcome(calls=(call("llm_busy"),), error=busy))

    assert (summary_row(summary_id).status, jobs_of(summary_id)) == ("writing", ["queued"])


def test_with_nothing_to_cite_nothing_is_asked_and_it_is_only_numbers() -> None:
    org = tiny_org()
    paid(org, day(-3))  # the week before: the issue is still open last week, with no quote

    summary_id = write(org, llm=None)

    found = summary_row(summary_id)
    assert (found.status, found.narrative, found.model) == ("ready", None, None)
    assert found.numbers is not None and found.numbers["issues"][0]["measures"]["insights"] == 0


def test_without_the_model_s_key_a_summary_with_quotes_fails_at_once_saying_so() -> None:
    org = tiny_org()
    paid(org, day(0))

    summary_id = write(org, llm=None)

    found = summary_row(summary_id)
    assert (found.status, found.failure_reason, jobs_of(summary_id)) == (
        "failed",
        "The summary's words need Muse Spark, which isn't set up on the server (MODEL_API_KEY)",
        ["dead"],
    )
