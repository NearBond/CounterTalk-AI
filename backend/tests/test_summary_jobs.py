"""Summaries asked for and written (batch 15b): one per team and period however often it's asked,
the job that counts and keeps its numbers, a job that dies, Try again, and the API."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from countertalk import jobs, summaries, summary_jobs, worker
from countertalk.db import session_scope
from countertalk.llm.client import MetaLlm
from countertalk.models import (
    Job,
    JobKind,
    LlmCall,
    LlmPurpose,
    Summary,
    Team,
    utc_now,
)
from tests.conftest import ORG_PACK_FIXTURES, lock_waiters
from tests.test_jobs import OTHER_WORKER, claimed, make_stale
from tests.test_review import PACK
from tests.test_review_api import error_of, signed_in_demo
from tests.test_roles import FakeLlm, tiny_org
from tests.test_summaries import visit_at
from tests.test_visits import Org_, make_org
from tests.test_worker import WORKER_ID, wait_for

NOW = utc_now()
LAST_WEEK = summaries.last_complete("week", NOW)


def team(org_id: str, key: str) -> str:
    with session_scope(read_only=True) as session:
        query = select(Team.id).where(Team.org_id == org_id, Team.key == key)
        return session.scalars(query).one()


def asked(org: Org_, key: str = "money") -> str:
    """Last week's summary of the tiny org's team: its ID."""
    with session_scope() as session:
        return summary_jobs.ask(session, org.org_id, "team", team(org.org_id, key), "week", NOW).id


def summary_row(summary_id: str) -> Summary:
    with session_scope(read_only=True) as session:
        return session.get_one(Summary, summary_id)


def jobs_of(summary_id: str) -> list[str]:
    """Its jobs' states, oldest first."""
    with session_scope(read_only=True) as session:
        query = select(Job.state).where(Job.summary_id == summary_id).order_by(Job.seq)
        return list(session.scalars(query))


def work() -> bool:
    """One turn of a worker that writes the tiny org's summaries, with an LLM whose narrative
    says nothing (test_narrative.py has what it says)."""
    llm = FakeLlm({"overview": [], "issues": []})
    handlers = {JobKind.SUMMARY: worker.summarise_with(llm, packs=ORG_PACK_FIXTURES)}
    return worker.Worker(WORKER_ID, handlers=handlers).run_once()


def page(org: Org_, key: str = "money") -> summary_jobs.Page:
    with session_scope(read_only=True) as session:
        return summary_jobs.page(
            session, lambda: PACK, org.org_id, "team", team(org.org_id, key), "week", NOW
        )


@pytest.fixture
def org() -> Org_:
    """The tiny org, with a payment at its shop last Monday."""
    org = tiny_org()
    monday = LAST_WEEK.start + timedelta(hours=6)
    visit_at(org, monday, monday)
    return org


# --- asked once (§2.5) -----------------------------------------------------------------------


def test_asking_twice_finds_the_one_summary_and_its_one_job(org: Org_) -> None:
    first = asked(org)

    assert asked(org) == first
    found = summary_row(first)
    assert (found.status, found.period_start, found.period_end) == (
        "writing",
        LAST_WEEK.start,
        LAST_WEEK.end,
    )
    assert jobs_of(first) == ["queued"]


def test_two_asks_at_once_make_one_summary_and_one_job(org: Org_) -> None:
    money = team(org.org_id, "money")

    def ask() -> str:
        with session_scope() as session:
            return summary_jobs.ask(session, org.org_id, "team", money, "week", NOW).id

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        first = summary_jobs.ask(session, org.org_id, "team", money, "week", NOW).id
        other = pool.submit(ask)
        wait_for(lambda: lock_waiters() == 1, "the second ask never waited for the first")
        session.commit()

        assert other.result(timeout=10) == first
    assert jobs_of(first) == ["queued"]


def test_a_team_s_and_the_org_s_summaries_are_apart(org: Org_) -> None:
    with session_scope() as session:
        whole = summary_jobs.ask(session, org.org_id, "org", None, "week", NOW).id
        month = summary_jobs.ask(session, org.org_id, "org", None, "month", NOW).id

    assert len({asked(org), asked(org, "field"), whole, month}) == 4


# --- written by its job ----------------------------------------------------------------------


def test_the_job_keeps_the_numbers_it_counted_and_the_page_shows_them(org: Org_) -> None:
    summary_id = asked(org)
    before = page(org)

    assert work()

    found = summary_row(summary_id)
    with session_scope(read_only=True) as session:
        counted = summaries.team_numbers(
            session, PACK, org.org_id, team(org.org_id, "money"), LAST_WEEK
        )
    numbers = summary_jobs.NUMBERS.dump_python(counted, mode="json")
    assert (found.status, found.numbers) == ("ready", numbers)
    assert counted.totals.insights == 1
    # Counted now while it's written, and its own once it's ready, as of when they were counted.
    assert (before.numbers, before.as_of, before.summary is not None) == (numbers, NOW, True)
    after = page(org)
    assert found.counted_at is not None and found.counted_at < found.updated_at
    assert (after.numbers, after.as_of) == (numbers, found.counted_at)
    assert jobs_of(summary_id) == ["done"]


def test_a_job_whose_numbers_don_t_add_up_fails_its_summary_and_try_again_writes_it(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary_id = asked(org)
    monkeypatch.setattr(summaries, "check", lambda *args: ["the team's totals are off"])

    work()

    found = summary_row(summary_id)
    assert (found.status, found.failure_reason, found.numbers) == (
        "failed",
        "The numbers didn't pass their checks",
        None,
    )
    # The numbers still show.
    assert page(org).numbers["totals"]["insights"] == 1

    monkeypatch.undo()
    with session_scope() as session:
        again = summary_jobs.retry(session, org.org_id, summary_id)
        assert (again.status, again.failure_reason) == ("writing", None)
    assert jobs_of(summary_id) == ["dead", "queued"]
    work()
    assert summary_row(summary_id).status == "ready"


def test_a_summary_job_stale_on_its_last_try_fails_its_summary(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(jobs.KINDS, JobKind.SUMMARY, 1)
    summary_id = asked(org)
    make_stale(claimed(OTHER_WORKER).id)

    worker.Worker(WORKER_ID).housekeep()

    found = summary_row(summary_id)
    assert (found.status, found.failure_reason) == ("failed", jobs.interrupted(1))


def test_try_again_leaves_a_summary_that_isn_t_failed_as_it_is(org: Org_) -> None:
    summary_id = asked(org)
    with session_scope() as session:
        assert summary_jobs.retry(session, org.org_id, summary_id).status == "writing"
    assert jobs_of(summary_id) == ["queued"]
    work()

    # A ready one isn't written (and paid for) again.
    with session_scope() as session:
        assert summary_jobs.retry(session, org.org_id, summary_id).status == "ready"

    assert jobs_of(summary_id) == ["done"]


def test_a_summary_s_job_log_lines_name_it(org: Org_, caplog: pytest.LogCaptureFixture) -> None:
    summary_id = asked(org)
    with session_scope(read_only=True) as session:
        job_id = session.scalars(select(Job.id).where(Job.summary_id == summary_id)).one()

    with caplog.at_level("INFO"):
        work()

    done = f"job done job={job_id} kind=summary visit=None summary={summary_id} attempt=1"
    assert done in [record.getMessage() for record in caplog.records]


# --- what the database holds (R9) ------------------------------------------------------------


def refused(row: Any) -> None:
    with pytest.raises(IntegrityError), session_scope() as session:
        session.add(row)
        session.flush()


def test_the_database_refuses_a_summary_without_its_team_or_with_one_for_the_org() -> None:
    org = tiny_org()
    money = team(org.org_id, "money")
    period = {"period_start": LAST_WEEK.start, "period_end": LAST_WEEK.end}

    refused(Summary(org_id=org.org_id, scope="team", team_id=None, **period))
    refused(Summary(org_id=org.org_id, scope="org", team_id=money, **period))
    refused(Summary(org_id=make_org("Another Org").org_id, scope="team", team_id=money, **period))


def test_the_database_refuses_a_summary_job_or_call_without_its_summary() -> None:
    with pytest.raises(IntegrityError), session_scope() as session:
        jobs.enqueue(session, JobKind.SUMMARY)
    refused(
        LlmCall(
            purpose=LlmPurpose.SUMMARY,
            provider="meta",
            model="m",
            prompt_hash="h",
            latency_ms=1,
            ok=True,
        )
    )


# --- the API ---------------------------------------------------------------------------------


def demo_team(client: TestClient, key: str) -> str:
    response = client.get("/api/issues/options")
    return next(t["id"] for t in response.json()["teams"] if t["key"] == key)


def test_the_api_asks_once_reads_the_numbers_at_once_and_tries_again(client: TestClient) -> None:
    demo = signed_in_demo(client)
    finance = demo_team(client, "finance")
    ask = {"scope": "team", "team_id": finance, "period": "week"}

    empty = client.get(
        "/api/summaries", params={"scope": "team", "team_id": finance, "period": "week"}
    )
    first = client.post("/api/summaries", json=ask)
    second = client.post("/api/summaries", json=ask)

    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert (body["summary"], body["numbers"]["totals"]["insights"]) == (None, 0)
    assert (datetime.fromisoformat(body["period_start"]), body["period"]) == (
        LAST_WEEK.start,
        "week",
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert (first.json()["status"], first.json()["team_id"]) == ("writing", finance)
    found = client.get(
        "/api/summaries", params={"scope": "team", "team_id": finance, "period": "week"}
    )
    assert found.json()["summary"] == first.json()
    summary_id = first.json()["id"]
    retried = client.post(f"/api/summaries/{summary_id}/retry")
    assert retried.json() == first.json()
    assert jobs_of(summary_id) == ["queued"]
    assert demo.org_id == summary_row(summary_id).org_id
    # Polled by its ID: ready, with the numbers the page then shows.
    assert worker.Worker(WORKER_ID).run_once()
    ready = client.get(f"/api/summaries/{summary_id}").json()
    page_now = client.get(
        "/api/summaries", params={"scope": "team", "team_id": finance, "period": "week"}
    ).json()
    assert (ready["status"], ready["numbers"]) == ("ready", body["numbers"])
    assert (page_now["numbers"], page_now["numbers_as_of"]) == (
        ready["numbers"],
        ready["counted_at"],
    )
    assert page_now["summary"] == ready


@pytest.mark.parametrize(
    ("ask", "expected"),
    [
        ({"scope": "org", "team_id": "TEAM", "period": "month"}, (400, "team_in_org_summary")),
        ({"scope": "team", "period": "week"}, (400, "team_needed")),
        ({"scope": "team", "team_id": "OTHER", "period": "week"}, (404, "team_not_found")),
    ],
)
def test_the_api_asks_only_for_its_own_org_s_teams(
    client: TestClient, ask: dict[str, Any], expected: tuple[int, str]
) -> None:
    signed_in_demo(client)
    other = team(tiny_org().org_id, "money")
    ids = {"TEAM": demo_team(client, "finance"), "OTHER": other}
    body = {**ask, **({"team_id": ids[ask["team_id"]]} if "team_id" in ask else {})}

    assert error_of(client.post("/api/summaries", json=body)) == expected
    params = {k: v for k, v in body.items() if v is not None}
    assert error_of(client.get("/api/summaries", params=params)) == expected


def test_no_request_calls_muse_spark(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # §2.4: the job writes the narrative; asking, reading and Try again only queue or read.
    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("Muse Spark was called inside a request")

    monkeypatch.setattr(MetaLlm, "complete", never)
    signed_in_demo(client)
    finance = demo_team(client, "finance")
    params = {"scope": "team", "team_id": finance, "period": "week"}

    asked_for = client.post("/api/summaries", json=params)
    summary_id = asked_for.json()["id"]
    responses = [
        asked_for,
        client.get("/api/summaries", params=params),
        client.get(f"/api/summaries/{summary_id}"),
        client.post(f"/api/summaries/{summary_id}/retry"),
    ]

    assert [r.status_code for r in responses] == [200, 200, 200, 200]


def test_another_org_s_summary_is_404(client: TestClient) -> None:
    signed_in_demo(client)
    theirs = asked(tiny_org())

    assert error_of(client.get(f"/api/summaries/{theirs}")) == (404, "summary_not_found")
    assert error_of(client.post(f"/api/summaries/{theirs}/retry")) == (404, "summary_not_found")
