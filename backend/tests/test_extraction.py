"""extraction.py and the extract job (batch 10), with a fake LLM that checks no transaction is
open while it's asked (§2.4). Request 0 is roles, request 1 extraction, unless roles are saved.
"""

import logging
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select, update

from countertalk import extraction, jobs, limits, visits, worker
from countertalk.db import session_scope
from countertalk.errors import Conflict, RetryableError
from countertalk.llm.base import Outcome
from countertalk.models import (
    Evidence,
    Extraction,
    Insight,
    Job,
    JobKind,
    JobState,
    LlmCall,
    Report,
    Task,
    Team,
    Visit,
    VisitClient,
    VisitKind,
    VisitStatus,
)
from countertalk.playbooks import Scheme, build_extraction_schema, playbook_text
from tests.conftest import ORG_PACK_FIXTURES, give_transcript
from tests.test_roles import ALL_PLACED, VISIT, FakeLlm, call, tiny, tiny_org
from tests.test_visits import Org_, load, move_to, new_visit

PACKS = {"packs": ORG_PACK_FIXTURES}
# 06:00 UTC: the same day in India.
VISITED = datetime(2026, 11, 15, 6, 0, tzinfo=UTC)


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def transcribed(org: Org_, kind: str = VisitKind.CONVERSATION) -> str:
    """A visit at `transcribed`, on VISITED, with batch 9's four turns: its ID."""
    visit_id = new_visit(org, kind=kind, client=VisitClient.UPLOAD)
    give_transcript(visit_id, VISIT)
    with session_scope() as session:
        session.execute(update(Visit).where(Visit.id == visit_id).values(created_at=VISITED))
    move_to(org, visit_id, VisitStatus.UPLOADED, VisitStatus.TRANSCRIBING, VisitStatus.TRANSCRIBED)
    return visit_id


def insight(**changes: Any) -> dict[str, Any]:
    found = {
        "kind": "product_request",
        "category": "demand",
        "severity": "low",
        "summary_en": "The shop wants two boxes.",
        "evidence": [{"turn_id": 1, "quote": "Kurkure ke do dabbe chahiye."}],
        "sku_mentions": ["Kurkure"],
        "competitor_mentions": [],
        "competitor_reason": None,
        "scheme_key": None,
        "suggested_team": "field",
        "team_reason": "Deliveries.",
    }
    return found | changes


def answer(*insights: dict[str, Any], **changes: Any) -> dict[str, Any]:
    task = {"type": "callback", "description_en": "Call back.", "due": "2026-11-17", "owner": "rep"}
    found = {
        "summary_en": "A routine order.",
        "next_visit_date": "2026-11-22",
        "insights": list(insights) if insights else [insight()],
        "tasks": [task],
    }
    return found | changes


def run(visit_id: str, *answers: dict[str, Any] | Outcome) -> FakeLlm:
    llm = FakeLlm(*answers)
    extraction.extract(visit_id, llm, **PACKS)
    return llm


def run_job(visit_id: str, llm: FakeLlm) -> Job:
    """The worker's way: the visit's extract job queued, claimed and run."""
    with session_scope() as session:
        jobs.enqueue(session, JobKind.EXTRACT, visit_id=visit_id)
    handlers = {JobKind.EXTRACT: worker.extract_with(llm, **PACKS)}
    assert worker.Worker("test-host:1:aaaaaa", handlers=handlers).run_once()
    with session_scope(read_only=True) as session:
        return session.scalars(select(Job).where(Job.kind == JobKind.EXTRACT)).one()


def rows(table: Any) -> list[Any]:
    with session_scope(read_only=True) as session:
        return list(session.scalars(select(table)))


def purposes(visit_id: str) -> list[tuple[str, bool]]:
    with session_scope(read_only=True) as session:
        query = select(LlmCall.purpose, LlmCall.ok).where(LlmCall.visit_id == visit_id)
        return [tuple(row) for row in session.execute(query.order_by(LlmCall.seq))]


def test_a_transcribed_visit_is_extracted_into_its_rows_and_goes_to_review(org: Org_) -> None:
    visit_id = transcribed(org)

    run(visit_id, ALL_PLACED, answer())

    assert load(visit_id).status == VisitStatus.REVIEW
    [run_row] = rows(Extraction)
    assert (run_row.visit_id, run_row.raw_json, run_row.model) == (
        visit_id,
        answer(),
        "fake-spark-1",
    )
    assert (run_row.playbook_id, run_row.playbook_version) == ("fmcg-kirana-visit", 1)
    assert (run_row.org_pack_id, run_row.org_pack_version) == ("tiny-demo", 1)
    assert run_row.prompt_hash == "ab" * 32
    [report] = rows(Report)
    assert (report.summary_en, report.next_visit_date) == ("A routine order.", date(2026, 11, 22))
    assert report.json == {"summary_en": "A routine order.", "next_visit_date": "2026-11-22"}
    [task] = rows(Task)
    assert (task.seq, task.type, task.due, task.owner, task.status) == (
        0,
        "callback",
        date(2026, 11, 17),
        "rep",
        "open",
    )
    [found] = rows(Insight)
    with session_scope(read_only=True) as session:
        field = session.scalars(select(Team.id).where(Team.key == "field")).one()
    assert (found.kind, found.category, found.severity) == ("product_request", "demand", "low")
    assert (found.sku_mentions, found.ai_team_id, found.ai_team_reason) == (
        ["Kurkure"],
        field,
        "Deliveries.",
    )
    # Its quote was found in the transcript (batch 11).
    assert (found.extraction_id, found.added_by_rep, found.verified) == (run_row.id, False, True)
    [quote] = rows(Evidence)
    assert (quote.turn_id, quote.speaker_role, quote.quote_llm) == (
        1,
        "shopkeeper",
        "Kurkure ke do dabbe chahiye.",
    )
    assert purposes(visit_id) == [("roles", True), ("extraction", True)]


def test_the_request_is_the_playbook_then_the_org_pack_then_the_transcript(org: Org_) -> None:
    visit_id = transcribed(org)
    llm = run(visit_id, ALL_PLACED, answer())

    [_, (messages, name, schema)] = llm.asked

    pack = tiny()
    playbook, company, visit = (m.content for m in messages)
    assert [m.role for m in messages] == ["system", "system", "user"]
    assert name == "extraction" and schema == build_extraction_schema(pack.playbook, pack)
    assert playbook.startswith(playbook_text(pack, "prompt"))
    for key, category in pack.playbook.categories.items():
        assert f"### `{key}`: {category.name}\n{category.definition}" in playbook
        assert all(f'- "{text}"' in playbook for text in category.includes)
        assert all(f"- {e.text} That's `{e.category}`." in playbook for e in category.excludes)
    assert company == (
        "# The company: Tiny Snacks (example)\n"
        "Made-up crisps sold to neighbourhood shops.\n"
        "\n## Its brands and products\n"
        "- Crunchy: Salted\n"
        "\n## Competitors' brands and products\n"
        "- Rival (Rival Foods)\n"
        "\n## Its teams, and what each handles\n"
        "- `money` (Money desk): Payments and credit notes.\n"
        "- `field` (Field team): Deliveries and the shops' visits.\n"
        "\n## Its schemes running within 30 days of the visit\n"
        "- `crunchy_winter`: Crunchy winter payout (payout, for Crunchy; 2026-11-01 to 2026-12-31)"
    )
    assert visit == (
        "The visit, on 2026-11-15:\n\n"
        "[T0][rep][00:01] Namaste Asha ji, aaj kya order hai?\n"
        "[T1][shopkeeper][00:04] Kurkure ke do dabbe chahiye.\n"
        "[T2][other][01:05] Bhaiya ek Thums Up dena.\n"
        "[T3][shopkeeper][01:10] Haan, yeh lo."
    )


def scheme(starts: str, ends: str) -> Scheme:
    return Scheme("s", "S", "payout", (), date.fromisoformat(starts), date.fromisoformat(ends), ())


@pytest.mark.parametrize(
    ("starts", "ends", "near"),
    [
        ("2026-12-15", "2027-01-31", True),  # starts 30 days after the visit
        ("2026-12-16", "2027-01-31", False),  # 31 days after
        ("2026-09-01", "2026-10-16", True),  # ended 30 days before
        ("2026-09-01", "2026-10-15", False),  # 31 days before
        ("2026-01-01", "2026-12-31", True),  # running on the day
    ],
)
def test_the_schemes_sent_are_those_running_within_30_days(
    starts: str, ends: str, near: bool
) -> None:
    found = scheme(starts, ends)

    assert extraction.schemes_near([found], date(2026, 11, 15)) == ([found] if near else [])


def test_an_unknown_team_and_impossible_dates_are_left_out_not_guessed(org: Org_) -> None:
    visit_id = transcribed(org)
    week_date = {"type": "callback", "description_en": "C.", "due": "2026-W46-1", "owner": "rep"}
    odd = answer(insight(suggested_team="sales"), next_visit_date="2026-02-30", tasks=[week_date])

    run(visit_id, ALL_PLACED, odd)

    [found] = rows(Insight)
    [report] = rows(Report)
    [task] = rows(Task)
    assert (found.ai_team_id, found.ai_team_reason, report.next_visit_date, task.due) == (
        None,
        "Deliveries.",
        None,
        None,
    )
    assert rows(Extraction)[0].raw_json["next_visit_date"] == "2026-02-30"  # kept as it came


def test_a_visit_with_nothing_raised_has_no_insights(org: Org_) -> None:
    visit_id = transcribed(org)

    run(visit_id, ALL_PLACED, {**answer(), "insights": [], "tasks": []})

    assert (rows(Insight), rows(Task), load(visit_id).status) == ([], [], VisitStatus.REVIEW)


def test_an_answer_still_invalid_after_its_repair_fails_the_visit(org: Org_) -> None:
    visit_id = transcribed(org)
    invalid = Outcome(
        calls=(call("llm_answer_invalid"), call("llm_answer_invalid")),
        error=RetryableError("llm_answer_invalid", "x"),  # the code is what counts
    )

    job = run_job(visit_id, FakeLlm(ALL_PLACED, invalid))

    visit = load(visit_id)
    assert (job.state, visit.status) == (JobState.DEAD, VisitStatus.FAILED)
    assert visit.failure_reason == (
        "Extraction invalid: Muse Spark's answer didn't fit, even after a repair"
    )
    # Both paid calls logged, and nothing dropped silently: no rows from a bad answer (§9.6).
    assert purposes(visit_id) == [("roles", True), ("extraction", False), ("extraction", False)]
    assert (rows(Extraction), rows(Insight)) == ([], [])


def test_a_retry_reuses_the_saved_roles(org: Org_) -> None:
    visit_id = transcribed(org)
    busy = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))
    with pytest.raises(RetryableError):
        run(visit_id, ALL_PLACED, busy)
    assert load(visit_id).status == VisitStatus.EXTRACTING

    retry = run(visit_id, answer())

    [(_, name, _)] = retry.asked  # extraction only: roles weren't asked again (§9.4)
    assert (name, load(visit_id).status) == ("extraction", VisitStatus.REVIEW)


def test_a_visit_that_moved_on_during_the_call_is_left_as_it_is(org: Org_) -> None:
    # The write re-checks under the visit's lock (§2.3): failed meanwhile, nothing is saved.
    visit_id = transcribed(org)

    def fail_it(request: int) -> None:
        if request == 1:
            with session_scope() as session:
                visits.fail_processing(session, visit_id, "Stopped by hand")

    run_with = FakeLlm(ALL_PLACED, answer(), during=fail_it)
    extraction.extract(visit_id, run_with, **PACKS)

    assert load(visit_id).status == VisitStatus.FAILED
    assert (rows(Extraction), rows(Report), rows(Insight)) == ([], [], [])


def test_extracting_again_replaces_the_rows_and_keeps_every_run(org: Org_) -> None:
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, answer())

    with session_scope() as session:
        assert extraction.queue_again(session, org.org_id, visit_id) is not None
    assert load(visit_id).status == VisitStatus.TRANSCRIBED
    again = answer(insight(category="supply", kind="problem"), insight(severity="high"))
    retry = run(visit_id, again)

    assert [name for _, name, _ in retry.asked] == ["extraction"]  # no roles, no speech-to-text
    assert len(rows(Extraction)) == 2
    with session_scope(read_only=True) as session:
        newest = session.scalars(select(Extraction.id).order_by(Extraction.seq.desc())).first()
    found = rows(Insight)
    assert sorted((i.seq, i.category, i.extraction_id) for i in found) == [
        (0, "supply", newest),
        (1, "demand", newest),
    ]
    assert (len(rows(Report)), len(rows(Task)), len(rows(Evidence))) == (1, 1, 2)


def test_a_confirmed_visit_is_never_extracted_again(org: Org_) -> None:
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, answer())
    move_to(org, visit_id, VisitStatus.CONFIRMED)

    with pytest.raises(Conflict) as error:
        with session_scope() as session:
            extraction.queue_again(session, org.org_id, visit_id)

    assert error.value.code == "visit_confirmed"
    assert len(rows(Extraction)) == 1


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (
            lambda org: transcribed(org, VisitKind.NOTES),
            "Notes visits aren't extracted yet",
        ),
        (transcribed, "The visit is too long for Muse Spark to read in one go"),
    ],
    ids=["notes", "too-long"],
)
def test_a_visit_it_cant_extract_fails_with_a_reason_and_nothing_paid(
    org: Org_, monkeypatch: pytest.MonkeyPatch, make: Any, reason: str
) -> None:
    monkeypatch.setattr(limits, "LLM_CONTEXT_TOKENS", limits.LLM_OUTPUT_RESERVE_TOKENS + 100)
    visit_id = make(org)
    llm = FakeLlm()

    job = run_job(visit_id, llm)

    visit = load(visit_id)
    assert (job.state, visit.status, visit.failure_reason) == (
        JobState.DEAD,
        VisitStatus.FAILED,
        reason,
    )
    assert (llm.asked, purposes(visit_id)) == ([], [])


def test_a_failure_retried_to_its_last_try_fails_the_visit(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    # §9.1: never left at extracting.
    monkeypatch.setitem(jobs.KINDS, JobKind.EXTRACT, 1)
    visit_id = transcribed(org)
    busy = Outcome(calls=(call("llm_unavailable"),), error=RetryableError("llm_unavailable", "B"))

    job = run_job(visit_id, FakeLlm(ALL_PLACED, busy))

    visit = load(visit_id)
    assert (job.state, visit.status) == (JobState.DEAD, VisitStatus.FAILED)
    assert visit.failure_reason == jobs.gave_up(1)


def test_without_a_key_the_visit_waits_at_transcribed(org: Org_) -> None:
    # MODEL_API_KEY is empty in tests (conftest), as in any process the builder starts.
    visit_id = transcribed(org)
    with session_scope() as session:
        jobs.enqueue(session, JobKind.EXTRACT, visit_id=visit_id)

    assert worker.Worker("test-host:1:aaaaaa").run_once()

    assert load(visit_id).status == VisitStatus.TRANSCRIBED
    with session_scope(read_only=True) as session:
        assert session.scalars(select(Job.state)).all() == [JobState.DONE]
    assert purposes(visit_id) == []


def test_housekeeping_queues_waiting_visits_a_few_at_a_time_once_a_key_is_set(
    org: Org_, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    waiting = [transcribed(org) for _ in range(3)]
    with session_scope() as session:
        jobs.enqueue(session, JobKind.EXTRACT, visit_id=waiting[0])  # has one already
    monkeypatch.setattr(limits, "EXTRACT_QUEUE_PER_PASS", 1)
    keyless = worker.Worker("test-host:1:aaaaaa").housekeep()["extractions_queued"]

    monkeypatch.setenv("MODEL_API_KEY", "not-a-real-key")
    from countertalk.config import get_settings

    get_settings.cache_clear()
    first = worker.Worker("test-host:1:aaaaaa").housekeep()["extractions_queued"]
    second = worker.Worker("test-host:1:aaaaaa").housekeep()["extractions_queued"]
    third = worker.Worker("test-host:1:aaaaaa").housekeep()["extractions_queued"]

    assert (keyless, first, second, third) == (0, 1, 1, 0)
    with session_scope(read_only=True) as session:
        queued = session.scalars(select(Job.visit_id).where(Job.kind == JobKind.EXTRACT)).all()
    assert sorted(queued) == sorted(waiting)


def test_the_worker_says_at_start_that_extraction_is_off(caplog: pytest.LogCaptureFixture) -> None:
    runner = worker.Worker("test-host:1:aaaaaa")
    runner.stopping.set()

    with caplog.at_level(logging.WARNING):
        runner.run()

    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "extraction is off: MODEL_API_KEY isn't set, so transcribed visits wait"
        " worker=test-host:1:aaaaaa"
    ]


def test_prompts_and_answers_are_never_logged(org: Org_, caplog: pytest.LogCaptureFixture) -> None:
    visit_id = transcribed(org)

    with caplog.at_level(logging.DEBUG):
        run(visit_id, ALL_PLACED, answer(insight(summary_en="Secret-summary")))

    assert "extracted visit=" in caplog.text  # it did log
    for words in ("Kurkure", "Asha", "Secret-summary", "Tiny Snacks", "codebook"):
        assert words not in caplog.text


def test_extracting_again_keeps_the_insights_the_rep_added(org: Org_) -> None:
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, answer())
    with session_scope() as session:
        session.add(
            Insight(
                org_id=org.org_id,
                visit_id=visit_id,
                seq=5,
                kind="problem",
                category="supply",
                severity="high",
                summary_en="Added by the rep.",
                sku_mentions=[],
                competitor_mentions=[],
                added_by_rep=True,
            )
        )
    with session_scope() as session:
        extraction.queue_again(session, org.org_id, visit_id)

    run(visit_id, answer(insight(), insight()))

    found = sorted((i.seq, i.added_by_rep) for i in rows(Insight))
    assert found == [(5, True), (6, False), (7, False)]  # numbered after the rep's


def test_a_nul_character_in_the_answer_is_dropped_not_a_failed_write(org: Org_) -> None:
    # Postgres refuses NUL in text and JSONB: kept, the write would fail on every paid retry.
    visit_id = transcribed(org)

    run(visit_id, ALL_PLACED, answer(insight(summary_en="Two\u0000 boxes.")))

    [found] = rows(Insight)
    assert found.summary_en == "Two boxes."
    assert rows(Extraction)[0].raw_json["insights"][0]["summary_en"] == "Two boxes."


@pytest.mark.parametrize(
    "steps",
    [
        [VisitStatus.UPLOADED, VisitStatus.TRANSCRIBING, VisitStatus.TRANSCRIBED],
        [VisitStatus.UPLOADED],
    ],
    ids=["transcribed", "uploaded"],
)
def test_a_visit_still_being_processed_isnt_queued_again(
    org: Org_, steps: list[VisitStatus]
) -> None:
    visit_id = new_visit(org, client=VisitClient.UPLOAD)
    give_transcript(visit_id, VISIT)
    move_to(org, visit_id, *steps)

    with pytest.raises(Conflict) as error:
        with session_scope() as session:
            extraction.queue_again(session, org.org_id, visit_id)

    assert error.value.code == "visit_processing"


def test_an_extraction_keeps_the_hash_of_its_request_not_of_its_repair(org: Org_) -> None:
    visit_id = transcribed(org)
    first = replace(call("llm_answer_invalid"), prompt_hash="aa" * 32)
    repair = replace(call(), prompt_hash="bb" * 32)

    run(visit_id, ALL_PLACED, Outcome(calls=(first, repair), answer=answer()))

    assert rows(Extraction)[0].prompt_hash == "aa" * 32


def test_without_a_key_a_visit_already_extracting_fails_clearly(org: Org_) -> None:
    visit_id = transcribed(org)
    move_to(org, visit_id, VisitStatus.EXTRACTING)
    with session_scope() as session:
        jobs.enqueue(session, JobKind.EXTRACT, visit_id=visit_id)

    assert worker.Worker("test-host:1:aaaaaa").run_once()

    visit = load(visit_id)
    assert (visit.status, visit.failure_reason) == (
        VisitStatus.FAILED,
        "Extraction stopped: the server has no MODEL_API_KEY",
    )


def test_a_queued_job_with_no_visit_doesnt_stop_housekeeping_queuing(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = transcribed(org)
    with session_scope() as session:
        jobs.enqueue(session, JobKind.EXTRACT)  # no visit: SQL's NOT IN would match nothing

    assert extraction.queue_waiting(limits.EXTRACT_QUEUE_PER_PASS) == 1

    with session_scope(read_only=True) as session:
        query = select(Job.visit_id).where(Job.kind == JobKind.EXTRACT, Job.visit_id.is_not(None))
        assert session.scalars(query).all() == [waiting]
