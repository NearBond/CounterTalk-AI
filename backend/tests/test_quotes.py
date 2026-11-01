"""quotes.py in the extract job: every quote checked, the unmatched repaired once (batch 11).

With batch 9's four turns (tests/test_roles.py VISIT): T0 the rep, T1 and T3 the shopkeeper, T2
someone else. Request 0 is roles, 1 extraction, then one per repair.
"""

import logging
from typing import Any

import pytest
from sqlalchemy import select

from countertalk import extraction
from countertalk.db import session_scope
from countertalk.errors import RetryableError
from countertalk.llm.base import Outcome
from countertalk.models import Evidence, Insight, VisitStatus
from countertalk.quotes import INSTRUCTIONS
from tests.test_extraction import PACKS, answer, insight, purposes, run, transcribed
from tests.test_roles import ALL_PLACED, FakeLlm, call, tiny_org
from tests.test_visits import Org_, load

ASKED = "Kurkure ke do dabbe chahiye."  # T1, the shopkeeper's
PARAPHRASE = "Kurkure ki do peti bhejo."


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def quotes_of(visit_id: str) -> list[tuple[Any, ...]]:
    with session_scope(read_only=True) as session:
        rows = session.scalars(
            select(Evidence).where(Evidence.visit_id == visit_id).order_by(Evidence.seq)
        )
        return [
            (
                e.turn_id,
                e.speaker_role,
                e.match,
                e.quote_final,
                e.char_start,
                e.char_end,
                e.start_ms,
            )
            for e in rows
        ]


def verified(visit_id: str) -> list[bool]:
    with session_scope(read_only=True) as session:
        query = select(Insight.verified).where(Insight.visit_id == visit_id).order_by(Insight.seq)
        return list(session.scalars(query))


def quoting(*quoted: tuple[int, str]) -> dict[str, Any]:
    return answer(insight(evidence=[{"turn_id": t, "quote": q} for t, q in quoted]))


# The words matched, from the transcript: its full stop is punctuation, so not part of it.
# T1 starts at 4 s: playback 1.5 s before it.
EXACT_T1 = (1, "shopkeeper", "exact", ASKED[:-1], 0, len(ASKED) - 1, 2_500)


def test_each_quote_is_written_with_what_checking_it_found(org: Org_) -> None:
    visit_id = transcribed(org)

    run(visit_id, ALL_PLACED, quoting((1, ASKED)))

    assert quotes_of(visit_id) == [EXACT_T1]
    assert verified(visit_id) == [True]


def test_a_quote_found_in_another_turn_takes_that_turn_and_its_role(org: Org_) -> None:
    visit_id = transcribed(org)

    run(visit_id, ALL_PLACED, quoting((0, ASKED)))  # cited as the rep's turn

    assert quotes_of(visit_id) == [EXACT_T1]


def test_a_quote_citing_a_turn_the_transcript_hasnt_got_is_searched_for(org: Org_) -> None:
    visit_id = transcribed(org)

    run(visit_id, ALL_PLACED, quoting((99, ASKED)))

    assert quotes_of(visit_id) == [EXACT_T1]


def test_an_unmatched_quote_gets_one_repair_whose_answer_is_checked_too(org: Org_) -> None:
    visit_id = transcribed(org)
    llm = FakeLlm(ALL_PLACED, quoting((1, PARAPHRASE)), {"turn_id": 1, "quote": ASKED})

    extraction.extract(visit_id, llm, **PACKS)

    [_, _, (messages, name, _)] = llm.asked
    assert name == "quote_repair"
    assert [(m.role, m.content) for m in messages] == [
        ("system", INSTRUCTIONS),
        (
            "user",
            "The point: The shop wants two boxes.\n\nThe turns:\n"
            "[T0][rep] Namaste Asha ji, aaj kya order hai?\n"
            "[T1][shopkeeper] Kurkure ke do dabbe chahiye.\n"
            "[T2][other] Bhaiya ek Thums Up dena.\n"
            "[T3][shopkeeper] Haan, yeh lo.",
        ),
    ]
    assert quotes_of(visit_id) == [EXACT_T1]
    assert purposes(visit_id) == [("roles", True), ("extraction", True), ("quote_repair", True)]


def test_a_repair_that_misses_too_leaves_the_quote_unmatched_and_isnt_tried_again(
    org: Org_,
) -> None:
    # §9.5: one repair per quote, whatever it brings back.
    visit_id = transcribed(org)
    llm = FakeLlm(ALL_PLACED, quoting((1, PARAPHRASE)), {"turn_id": 1, "quote": PARAPHRASE})

    extraction.extract(visit_id, llm, **PACKS)

    assert len(llm.asked) == 3
    assert quotes_of(visit_id) == [(1, "shopkeeper", "none", None, None, None, None)]
    assert verified(visit_id) == [False]


def test_a_failed_repair_leaves_the_quote_unmatched_and_never_fails_the_visit(
    org: Org_,
) -> None:
    visit_id = transcribed(org)
    busy = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))

    run(visit_id, ALL_PLACED, quoting((1, PARAPHRASE)), busy)

    assert load(visit_id).status == VisitStatus.REVIEW
    assert quotes_of(visit_id) == [(1, "shopkeeper", "none", None, None, None, None)]
    assert purposes(visit_id)[-1] == ("quote_repair", False)  # logged, though it failed


def test_one_matched_quote_verifies_its_insight_and_only_it_has_text_to_show(org: Org_) -> None:
    visit_id = transcribed(org)
    busy = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))

    run(visit_id, ALL_PLACED, quoting((1, ASKED), (3, PARAPHRASE)), busy)

    assert verified(visit_id) == [True]
    assert quotes_of(visit_id) == [EXACT_T1, (3, "shopkeeper", "none", None, None, None, None)]


def test_checking_again_gives_the_same_quotes(org: Org_) -> None:
    # §9.4: re-extraction replaces the rows with the same results.
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, quoting((0, ASKED), (3, "Haan yeh lo")))
    first = quotes_of(visit_id)
    with session_scope() as session:
        extraction.queue_again(session, org.org_id, visit_id)

    run(visit_id, quoting((0, ASKED), (3, "Haan yeh lo")))

    assert quotes_of(visit_id) == first
    with session_scope(read_only=True) as session:
        assert len(session.scalars(select(Evidence)).all()) == 2


def test_quotes_are_never_logged(org: Org_, caplog: pytest.LogCaptureFixture) -> None:
    visit_id = transcribed(org)
    busy = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))

    with caplog.at_level(logging.DEBUG):
        run(visit_id, ALL_PLACED, quoting((1, ASKED), (3, PARAPHRASE)), busy)

    assert [r.getMessage() for r in caplog.records if r.name == "countertalk.quotes"] == [
        f"quotes checked visit={visit_id} exact=1 fuzzy=0 none=1 repairs=1 unrepairable=0"
    ]
    for words in ("Kurkure", "dabbe", "peti", "Haan"):
        assert words not in caplog.text


def test_an_unmatched_quote_citing_no_turn_is_kept_with_none(org: Org_) -> None:
    # No cited turn to show a repair: nothing is sent, and the quote is kept, never dropped.
    visit_id = transcribed(org)
    llm = FakeLlm(ALL_PLACED, quoting((99, PARAPHRASE)))

    extraction.extract(visit_id, llm, **PACKS)

    assert len(llm.asked) == 2
    assert quotes_of(visit_id) == [(None, None, "none", None, None, None, None)]


def test_a_repair_answer_of_the_wrong_shape_leaves_the_quote_unmatched(org: Org_) -> None:
    visit_id = transcribed(org)

    run(visit_id, ALL_PLACED, quoting((1, PARAPHRASE)), {"turn_id": "one", "quote": ASKED})

    assert quotes_of(visit_id) == [(1, "shopkeeper", "none", None, None, None, None)]


def test_a_lost_claim_during_a_repair_stops_the_job_before_it_writes(org: Org_) -> None:
    # Another worker may have the job now (§2.4): this one mustn't write the visit's rows.
    visit_id = transcribed(org)
    lost = Outcome(calls=(), error=RetryableError("claim_lost", "Lost"))

    with pytest.raises(RetryableError) as error:
        run(visit_id, ALL_PLACED, quoting((1, PARAPHRASE)), lost)

    assert error.value.code == "claim_lost"
    assert (quotes_of(visit_id), load(visit_id).status) == ([], VisitStatus.EXTRACTING)


def test_after_a_repair_fails_to_reach_meta_the_rest_arent_sent(org: Org_) -> None:
    # Meta down: one failed repair, not one per quote, each waiting out its timeout.
    visit_id = transcribed(org)
    busy = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))
    llm = FakeLlm(ALL_PLACED, quoting((1, PARAPHRASE), (3, "Haan ji theek hai bhai")), busy)

    extraction.extract(visit_id, llm, **PACKS)

    assert len(llm.asked) == 3
    assert [q[2] for q in quotes_of(visit_id)] == ["none", "none"]
