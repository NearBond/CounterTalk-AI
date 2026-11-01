"""scripts/assign_roles.py: a dry run, then roles for real (batch 9), with a fake LLM.

It prints labels, roles, counts and costs, never the transcript's words.
"""

import json
import math

import pytest

from countertalk import roles
from countertalk.errors import RetryableError
from countertalk.llm.base import Outcome
from scripts.assign_roles import (
    CHARS_PER_TOKEN,
    OUTPUT_TOKENS_PER_LABEL,
    REASONING_TOKENS,
    USD_PER_M_INPUT,
    USD_PER_M_OUTPUT,
    cost,
    main,
)
from tests.conftest import ORG_PACK_FIXTURES
from tests.test_roles import (
    ALL_PLACED,
    VISIT,
    FakeLlm,
    call,
    saved,
    tiny,
    tiny_org,
    transcribed,
    visit_of,
)
from tests.test_visits import Org_, make_org, new_visit

ROOT = ["--root", str(ORG_PACK_FIXTURES)]


@pytest.fixture
def org() -> Org_:
    return tiny_org()


WORDS = ("Namaste", "Kurkure", "Thums", "Haan")


def run(
    capsys: pytest.CaptureFixture[str], args: list[str], llm: FakeLlm | None
) -> tuple[int, str, str]:
    code = main([*args, *ROOT], llm=llm)
    out, err = capsys.readouterr()
    return code, out, err


def test_a_dry_run_says_what_would_be_sent_and_sends_nothing(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript_id = transcribed(org, VISIT)
    visit_id = visit_of(transcript_id)
    llm = FakeLlm()

    code, out, _ = run(capsys, [visit_id], llm)

    request = roles.request_for(transcript_id, tiny())
    chars = sum(len(m.content) for m in request.messages) + len(json.dumps(request.schema))
    tokens_in = math.ceil(chars / CHARS_PER_TOKEN)
    tokens_out = 3 * OUTPUT_TOKENS_PER_LABEL + REASONING_TOKENS
    each = (tokens_in * USD_PER_M_INPUT + tokens_out * USD_PER_M_OUTPUT) / 1_000_000
    assert (code, llm.asked) == (0, [])
    assert out.splitlines() == [
        f"visit {visit_id}: 4 turns, 3 labels: c0:A, c0:B, c0:C",
        f"to send: 4 turns, {chars:,} characters with the instructions and schema",
        f"about {tokens_in:,} input and {tokens_out:,} output tokens, ${each:.4f} a request"
        " (an estimate: a re-ask or a repair sends another; --yes sends it)",
    ]
    assert not any(word in out for word in WORDS)


def test_yes_saves_the_roles_and_prints_each_calls_tokens_and_cost(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    transcript_id = transcribed(org, VISIT)
    visit_id = visit_of(transcript_id)

    code, out, _ = run(capsys, [visit_id, "--yes"], FakeLlm(ALL_PLACED))

    assert (code, out.splitlines()) == (
        0,
        [
            f"visit {visit_id}: roles for 3 labels",
            "  c0:A    rep         0.90",
            "  c0:B    shopkeeper  0.80",
            "  c0:C    other       0.70",
            f"call 1: ok, 900 input (0 cached), 60 output (20 reasoning), 1.2 s, ${ONE_CALL:.4f}",
            f"cost: ${ONE_CALL:.4f}",
        ],
    )
    assert saved(transcript_id)["c0:A"][:2] == ("rep", 0.9)


def test_roles_saved_already_are_printed_and_nothing_is_sent(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = visit_of(transcribed(org, VISIT))
    run(capsys, [visit_id, "--yes"], FakeLlm(ALL_PLACED))
    again = FakeLlm()

    code, out, _ = run(capsys, [visit_id, "--yes"], again)

    assert (code, again.asked) == (0, [])
    assert out.splitlines() == [
        f"visit {visit_id}: roles saved already, nothing sent",
        "  c0:A    rep         0.90",
        "  c0:B    shopkeeper  0.80",
        "  c0:C    other       0.70",
    ]


ONE_CALL = (900 * USD_PER_M_INPUT + 60 * USD_PER_M_OUTPUT) / 1_000_000


def test_a_failed_run_prints_what_it_paid_for_then_one_line(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    # Placed nobody twice: two calls paid for, and no roles.
    visit_id = visit_of(transcribed(org, VISIT))
    llm = FakeLlm({"speakers": []}, {"speakers": []})

    code, out, err = run(capsys, [visit_id, "--yes"], llm)

    line = f"900 input (0 cached), 60 output (20 reasoning), 1.2 s, ${ONE_CALL:.4f}"
    assert (code, out.splitlines(), err) == (
        1,
        [f"call 1: ok, {line}", f"call 2: ok, {line}", f"cost: ${2 * ONE_CALL:.4f}"],
        "failed: roles_none_placed: Muse Spark didn't say who any speaker was\n",
    )


def test_a_failed_call_is_shown_with_its_code(
    org: Org_, capsys: pytest.CaptureFixture[str]
) -> None:
    visit_id = visit_of(transcribed(org, VISIT))
    busy = RetryableError("llm_unavailable", "Muse Spark is busy")

    code, out, err = run(
        capsys, [visit_id, "--yes"], FakeLlm(Outcome((call(busy.code),), error=busy))
    )

    assert (code, err) == (1, "failed: llm_unavailable: Muse Spark is busy\n")
    assert out.splitlines()[0].startswith("call 1: llm_unavailable, 900 input")


def test_ctrl_c_during_a_call_is_one_line(org: Org_, capsys: pytest.CaptureFixture[str]) -> None:
    visit_id = visit_of(transcribed(org, VISIT))

    def stop(request: int) -> None:
        raise KeyboardInterrupt

    code, _, err = run(capsys, [visit_id, "--yes"], FakeLlm(ALL_PLACED, during=stop))

    assert (code, err) == (130, "stopped: a call in flight may still be billed; no roles saved\n")


def test_cost_counts_cached_input_at_its_own_price() -> None:
    # 600k input at $1.25/M, 400k cached at $0.15/M, 1M output at $4.25/M (reasoning in it).
    assert cost(1_000_000, 400_000, 1_000_000) == pytest.approx(0.75 + 0.06 + 4.25)


def test_yes_without_a_key_sends_nothing(org: Org_, capsys: pytest.CaptureFixture[str]) -> None:
    # MODEL_API_KEY is empty in tests (conftest), as in any process the builder starts.
    visit_id = visit_of(transcribed(org, VISIT))

    code, _, err = run(capsys, [visit_id, "--yes"], None)

    assert (code, err) == (1, "MODEL_API_KEY isn't set in backend/.env\n")


@pytest.mark.parametrize("has_transcript", [False, True], ids=["no-transcript", "no-org-pack"])
def test_a_visit_it_cant_do_is_one_line(
    capsys: pytest.CaptureFixture[str], has_transcript: bool
) -> None:
    bare = make_org()  # on no org pack
    visit_id = new_visit(bare)
    if has_transcript:
        transcribed_for = transcribed(bare, VISIT)
        visit_id = visit_of(transcribed_for)

    code, _, err = run(capsys, [visit_id], FakeLlm())

    why = "its org isn't on an org pack" if has_transcript else "it has no transcript"
    assert (code, err) == (1, f"can't give visit {visit_id} roles: {why}\n")
