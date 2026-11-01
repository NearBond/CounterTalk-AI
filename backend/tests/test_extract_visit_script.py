"""scripts/extract_visit.py: a dry run, then the extract job the worker's way (batch 10), with a
fake LLM. It prints counts, kinds, categories, severities and costs: never the words.
"""

import pytest

from countertalk.errors import RetryableError
from countertalk.llm import pricing
from countertalk.llm.base import Outcome
from countertalk.models import VisitStatus
from scripts.extract_visit import ANSWER_TOKENS, main
from tests.conftest import ORG_PACK_FIXTURES
from tests.test_extraction import answer, insight, transcribed
from tests.test_roles import ALL_PLACED, FakeLlm, call, tiny, tiny_org
from tests.test_visits import Org_, load, move_to

ROOT = ["--root", str(ORG_PACK_FIXTURES)]
WORDS = ("Kurkure", "Namaste", "two boxes", "Deliveries", "routine")


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def run(
    capsys: pytest.CaptureFixture[str], args: list[str], llm: FakeLlm | None
) -> tuple[int, str, str]:
    code = main([*args, *ROOT], llm=llm)
    out, err = capsys.readouterr()
    return code, out, err


def test_a_dry_run_says_what_would_be_sent_and_sends_nothing(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = transcribed(org)
    llm = FakeLlm()

    code, out, _ = run(capsys, [visit_id], llm)

    from countertalk import extraction

    request = extraction.request_for(visit_id, tiny(), {})
    tokens_in = pricing.input_tokens(request.messages, request.schema)
    tokens_out = pricing.output_tokens(ANSWER_TOKENS)
    assert (code, llm.asked) == (0, [])
    assert out.splitlines() == [
        f"visit {visit_id}: 4 turns, on 2026-11-15",
        "roles: to ask first (see assign_roles.py)",
        f"to send: {pricing.request_chars(request.messages, request.schema):,} characters;"
        f" about {tokens_in:,} input and {tokens_out:,} output tokens (reasoning counted),"
        f" ${pricing.cost(tokens_in, 0, tokens_out):.4f} (an estimate: --yes sends it)",
    ]
    assert load(visit_id).status == VisitStatus.TRANSCRIBED


def test_yes_runs_the_visits_extract_job_and_prints_no_words(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = transcribed(org)

    code, out, _ = run(capsys, [visit_id, "--yes"], FakeLlm(ALL_PLACED, answer()))

    each = pricing.call_cost(call())
    paid = f"900 input (0 cached), 60 output (20 reasoning), 1.2 s, ${each:.4f}"
    assert (code, load(visit_id).status) == (0, VisitStatus.REVIEW)
    assert out.splitlines() == [
        f"visit {visit_id}: review, 1 insights, 1 tasks",
        "  #0 product_request demand low, team field, scheme -, 1 quotes",
        f"call 1: roles ok, {paid}",
        f"call 2: extraction ok, {paid}",
        f"cost: ${2 * each:.4f}",
    ]
    assert not any(word in out for word in WORDS)


def test_again_extracts_a_visit_in_review_with_no_roles_paid_for(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = transcribed(org)
    run(capsys, [visit_id, "--yes"], FakeLlm(ALL_PLACED, answer()))
    without_again = run(capsys, [visit_id], FakeLlm())
    again = FakeLlm(answer(insight(), insight(severity="high")))

    code, out, _ = run(capsys, [visit_id, "--yes", "--again"], again)

    assert without_again[1] == f"visit {visit_id} is review: --again extracts it again\n"
    assert [name for _, name, _ in again.asked] == ["extraction"]
    assert (code, out.splitlines()[0]) == (0, f"visit {visit_id}: review, 2 insights, 1 tasks")


def test_again_on_a_confirmed_visit_is_one_line(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = transcribed(org)
    run(capsys, [visit_id, "--yes"], FakeLlm(ALL_PLACED, answer()))
    move_to(org, visit_id, VisitStatus.CONFIRMED)

    code, _, err = run(capsys, [visit_id, "--yes", "--again"], FakeLlm())

    assert (code, err) == (
        1,
        "failed: visit_confirmed: A confirmed visit can't be extracted again\n",
    )


def test_yes_without_a_key_sends_nothing(org: Org_, capsys: pytest.CaptureFixture[str]) -> None:
    visit_id = transcribed(org)

    code, _, err = run(capsys, [visit_id, "--yes"], None)

    assert (code, err) == (1, "MODEL_API_KEY isn't set in backend/.env\n")
    assert load(visit_id).status == VisitStatus.TRANSCRIBED


def test_again_never_pays_for_roles_that_arent_saved(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = transcribed(org)
    move_to(org, visit_id, VisitStatus.FAILED, reason="Stopped")  # before roles were asked
    llm = FakeLlm()

    code, _, err = run(capsys, [visit_id, "--yes", "--again"], llm)

    assert (code, llm.asked) == (1, [])
    assert err == f"visit {visit_id}'s roles aren't saved, and --again never pays for them\n"


def test_a_failure_to_be_retried_says_so(org: Org_, capsys: pytest.CaptureFixture[str]) -> None:
    visit_id = transcribed(org)
    busy = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))

    code, _, err = run(capsys, [visit_id, "--yes"], FakeLlm(ALL_PLACED, busy))

    assert (code, err) == (1, "job queued: RetryableError: llm_unavailable: B\n")
    assert load(visit_id).status == VisitStatus.EXTRACTING
