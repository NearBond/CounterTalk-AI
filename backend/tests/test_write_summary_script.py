"""scripts/write_summary.py and load_patterns.py (batch 15b): a dry run, then the summary job
the worker's way, with a fake LLM; and K6 loaded into a dev org once. They print counts and
costs: never the words. And DEV_USER_EMAIL, to see that org in the app: development only."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from countertalk.config import get_settings
from countertalk.db import session_scope
from countertalk.errors import PermanentError
from countertalk.llm import pricing
from countertalk.llm.base import Outcome
from countertalk.main import create_app
from countertalk.models import Org, Summary, User, ist_date
from scripts import load_patterns, write_summary
from tests.conftest import ORG_PACK_FIXTURES
from tests.test_narrative import day, paid, request, said
from tests.test_review_api import seeded_demo
from tests.test_roles import FakeLlm, call, tiny_org
from tests.test_summary_jobs import LAST_WEEK, NOW, summary_row
from tests.test_visits import Org_

ROOT = ["--root", str(ORG_PACK_FIXTURES)]
SENTENCE = "1 shop is waiting on its payment."
WORDS = (SENTENCE, "Kurkure", "Payments.")


def run(
    capsys: pytest.CaptureFixture[str], org: Org_, *args: str, llm: FakeLlm | None = None
) -> tuple[int, str, str]:
    argv = [org.org_id, "--team", "money", "--period", "week", *args, *ROOT]
    code = write_summary.main(argv, llm=llm, now=NOW)
    out, err = capsys.readouterr()
    return code, out, err


def summaries_of(org: Org_) -> int:
    with session_scope(read_only=True) as session:
        return session.scalar(select(func.count()).where(Summary.org_id == org.org_id)) or 0


def only_summary(org: Org_) -> str:
    with session_scope(read_only=True) as session:
        return session.scalars(select(Summary.id).where(Summary.org_id == org.org_id)).one()


def test_a_dry_run_says_what_would_be_sent_and_sends_and_writes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    org = tiny_org()
    paid(org, day(0))
    llm = FakeLlm()

    code, out, _ = run(capsys, org, llm=llm)

    found = request(org)
    cost = pricing.cost(
        pricing.input_tokens(found.messages, found.schema),
        0,
        pricing.output_tokens(write_summary.ANSWER_TOKENS),
    )
    tokens_in = pricing.input_tokens(found.messages, found.schema)
    tokens_out = pricing.output_tokens(write_summary.ANSWER_TOKENS)
    first, last = ist_date(LAST_WEEK.start), ist_date(LAST_WEEK.end - timedelta(days=1))
    assert (code, llm.asked, summaries_of(org)) == (0, [], 0)
    assert out.splitlines() == [
        f"team summary of the week {first.isoformat()} to {last.isoformat()}:"
        " 1 insights at 1 shops",
        f"to send: 1 issues, 1 quotes; {pricing.request_chars(found.messages, found.schema):,}"
        f" characters; about {tokens_in:,} input and {tokens_out:,} output tokens"
        f" (reasoning counted), ${cost:.4f} (an estimate: --yes sends it)",
    ]


def test_a_dry_run_with_nothing_to_cite_says_it_pays_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    org = tiny_org()

    code, out, _ = run(capsys, org)

    assert (code, out.splitlines()[-1]) == (
        0,
        "nothing to cite: --yes would keep only its numbers, and pay nothing",
    )


def test_yes_writes_it_the_worker_s_way_prints_no_words_and_never_pays_twice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    org = tiny_org()
    inside = paid(org, day(0))
    llm = FakeLlm({"overview": [said(SENTENCE, inside)], "issues": []})

    code, out, err = run(capsys, org, "--yes", llm=llm)
    again, out_again, _ = run(capsys, org, "--yes", llm=FakeLlm())

    summary_id = only_summary(org)
    assert (code, err, len(llm.asked)) == (0, "", 1)
    assert out.splitlines() == [
        f"summary {summary_id}: ready, 1 sentences kept by fake-spark-1;"
        " dropped: extra 0, uncited 0, citation 0, number 0",
        "call 1: summary ok, 900 input (0 cached), 60 output (20 reasoning), 1.2 s,"
        f" ${pricing.cost(900, 0, 60):.4f}",
        f"cost: ${pricing.cost(900, 0, 60):.4f}",
    ]
    for words in WORDS:
        assert words not in out + err
    assert (again, out_again) == (0, f"summary {summary_id} is written already: nothing sent\n")


def test_yes_tries_a_failed_summary_again(capsys: pytest.CaptureFixture[str]) -> None:
    org = tiny_org()
    inside = paid(org, day(0))
    invalid = PermanentError("llm_answer_invalid", "The answer didn't match its schema")
    run(
        capsys,
        org,
        "--yes",
        llm=FakeLlm(Outcome(calls=(call("llm_answer_invalid"),), error=invalid)),
    )
    summary_id = only_summary(org)
    assert summary_row(summary_id).status == "failed"

    code, _, _ = run(
        capsys, org, "--yes", llm=FakeLlm({"overview": [said(SENTENCE, inside)], "issues": []})
    )

    assert (code, summary_row(summary_id).status) == (0, "ready")


def test_a_dry_run_of_a_written_summary_says_yes_sends_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    org = tiny_org()
    inside = paid(org, day(0))
    run(capsys, org, "--yes", llm=FakeLlm({"overview": [said(SENTENCE, inside)], "issues": []}))

    code, out, _ = run(capsys, org)

    assert (code, out.splitlines()[1:]) == (
        0,
        [f"summary {only_summary(org)} is written already: --yes sends nothing"],
    )


def test_a_world_loaded_part_way_is_refused_not_taken_as_loaded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with session_scope() as session:
        org = Org(name=f"{load_patterns.NAME} stopped part-way")
        session.add(org)
        session.flush()
        org_id = org.id

    assert load_patterns.main([]) == 1

    assert capsys.readouterr().err == (
        f"org {org_id} has 0 of the world's 67 visits: a load stopped part-way,"
        " so load it into a fresh database\n"
    )


def test_yes_without_a_key_sends_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    org = tiny_org()
    paid(org, day(0))

    code, _, err = run(capsys, org, "--yes")

    assert (code, err, summaries_of(org)) == (1, "MODEL_API_KEY isn't set in backend/.env\n", 0)


def test_an_unknown_team_is_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    org = tiny_org()

    code = write_summary.main([org.org_id, "--team", "nobody", "--period", "week", *ROOT])

    assert (code, capsys.readouterr().err) == (1, f"org {org.org_id} has no team nobody\n")


def test_k6_loads_into_an_org_of_its_own_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert load_patterns.main([]) == 0
    first = capsys.readouterr().out
    org_id = first.split()[2].rstrip(",")

    assert load_patterns.main([]) == 0

    see = f"to see it in the app: DEBUG=1 and DEV_USER_EMAIL=finance-{org_id[:8]}@example.com\n"
    assert first == f"loaded: org {org_id}, 30 shops, 67 visits\n{see}"
    assert capsys.readouterr().out == f"loaded already: org {org_id}\n{see}"


# --- DEV_USER_EMAIL: the app as an example org's member, in development only ------------------


def test_dev_user_email_is_refused_without_debug_and_the_app_won_t_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEBUG", "0")
    monkeypatch.setenv("DEV_USER_EMAIL", "finance-0000@example.com")
    get_settings.cache_clear()

    with pytest.raises(ValidationError) as refused:
        create_app()

    assert "DEV_USER_EMAIL is for development only: set DEBUG=1 with it, or leave it empty" in str(
        refused.value
    )


def test_with_debug_the_api_acts_as_dev_user_email_s_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded_demo()
    org = tiny_org()
    with session_scope(read_only=True) as session:
        email = session.get_one(User, org.rep_id).email
    monkeypatch.setenv("DEBUG", "1")
    monkeypatch.setenv("DEV_USER_EMAIL", email)
    get_settings.cache_clear()

    with TestClient(create_app()) as client:
        response = client.get("/api/shops")

    assert response.status_code == 200, response.text
    # The tiny org's one shop, not the demo org's.
    assert [shop["name"] for shop in response.json()["items"]] == ["Test Shop"]
