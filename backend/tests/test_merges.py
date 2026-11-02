"""Merge suggestions (batch 15c): asked once in a team summary's job, for each category with two
or more active issues; code keeps only what holds, stores each pair once, and a dismissed pair is
never suggested again. The AI suggests; a person decides."""

import json
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import select

from countertalk import issues, merges, summary_jobs, worker
from countertalk.db import session_scope
from countertalk.errors import Conflict, NotFound, RetryableError
from countertalk.llm.base import Message, Outcome
from countertalk.models import Insight, Issue, IssueMergeSuggestion, Job, LlmCall, Visit
from scripts import _k6
from tests.conftest import ORG_PACK_FIXTURES, lock_waiters, open_transactions
from tests.test_issue_changes import change, now_of
from tests.test_jobs import make_due
from tests.test_merge import LOW, money_issue, two_issues
from tests.test_narrative import at_shop, day
from tests.test_review import PACK
from tests.test_roles import call, tiny_org
from tests.test_summaries import issue_of as k6_issue_of
from tests.test_summaries import visit_at
from tests.test_summary_jobs import WORKER_ID, asked, jobs_of, summary_row, team
from tests.test_visits import Org_
from tests.test_worker import wait_for

Answer = dict[str, Any] | Outcome


@pytest.fixture
def two() -> tuple[Org_, str, str, str]:
    return two_issues()


class ByName:
    """Answers each request by its name (merge suggestions, or the narrative): an answer, an
    Outcome, or a function of the request's payload."""

    provider = "fake"
    model = "fake-spark-1"

    def __init__(self, **answers: Answer | Callable[[dict[str, Any]], Answer]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, dict[str, Any]]] = []

    def complete(
        self,
        messages: Sequence[Message],
        name: str,
        schema: dict[str, Any],
        *,
        claim_lost: threading.Event | None = None,
    ) -> Outcome:
        assert open_transactions() == 0, "a transaction was open during an LLM call"
        payload = json.loads(messages[1].content)
        self.asked.append((name, payload))
        found = self.answers[name]
        answer = found(payload) if callable(found) else found
        return answer if isinstance(answer, Outcome) else Outcome(calls=(call(),), answer=answer)


NOTHING = {"overview": [], "issues": []}


def suggesting(*found: dict[str, Any]) -> dict[str, Any]:
    return {"suggestions": list(found)}


def write(org: Org_, llm: ByName) -> str:
    """Last week's money summary, asked for and written by one worker turn: its ID."""
    summary_id = asked(org)
    handlers = {"summary": worker.summarise_with(llm, packs=ORG_PACK_FIXTURES)}
    worker.Worker(WORKER_ID, handlers=handlers).run_once()
    return summary_id


def suggestions_of(org: Org_) -> list[tuple[str, str, str, str]]:
    with session_scope(read_only=True) as session:
        found = session.scalars(
            select(IssueMergeSuggestion).where(IssueMergeSuggestion.org_id == org.org_id)
        ).all()
        return [(s.into_issue_id, s.merge_issue_id, s.status, s.reason) for s in found]


# --- what's asked ----------------------------------------------------------------------------


def test_each_category_with_two_active_issues_is_asked_with_titles_and_findings(
    two: tuple[Org_, str, str, str],
) -> None:
    org, low, high, _ = two
    visit_at(org, day(1), day(1), LOW)  # the low issue's newer finding
    with session_scope(read_only=True) as session:
        [ask] = merges.asks_for(session, PACK, org.org_id, team(org.org_id, "money"))
        # field has the high payment alone: nothing to ask there.
        assert merges.asks_for(session, PACK, org.org_id, team(org.org_id, "field")) == []

    payload = json.loads(ask.messages[1].content)
    assert (ask.category, ask.issue_ids) == ("payments", (low, high))
    assert [i["id"] for i in payload["issues"]] == [low, high]
    assert payload["issues"][0]["findings"] == ["The shop wants two boxes."] * 2
    assert ask.schema["properties"]["suggestions"]["items"]["properties"]["into"]["enum"] == [
        low,
        high,
    ]


# --- what's kept -----------------------------------------------------------------------------


def test_a_kept_suggestion_is_stored_open_and_its_cost_counted(
    two: tuple[Org_, str, str, str],
) -> None:
    org, low, high, _ = two
    llm = ByName(
        merge_suggestions=suggesting({"into": low, "merge": [high], "reason": " Same payment. "}),
        summary_narrative=NOTHING,
    )

    summary_id = write(org, llm)

    assert suggestions_of(org) == [(low, high, "open", "Same payment.")]
    assert summary_row(summary_id).merge_step == {
        "asked": 1,
        "failed": 0,
        "kept": 1,
        "dropped": {"empty": 0, "outside": 0, "twice": 0, "reason": 0, "closed": 0, "already": 0},
    }
    with session_scope(read_only=True) as session:
        purposes = session.scalars(
            select(LlmCall.purpose).where(LlmCall.summary_id == summary_id).order_by(LlmCall.seq)
        ).all()
    assert purposes == ["merge", "summary"]


def test_code_drops_a_suggestion_naming_an_issue_not_given_twice_or_merging_nothing(
    two: tuple[Org_, str, str, str],
) -> None:
    org, low, high, _ = two
    stranger = str(uuid.uuid4())
    llm = ByName(
        merge_suggestions=suggesting(
            {"into": low, "merge": [stranger], "reason": "Not given."},
            {"into": low, "merge": [], "reason": "Nothing."},
            {"into": high, "merge": [low], "reason": "One way."},
            {"into": low, "merge": [high], "reason": "And back: a chain."},
        ),
        summary_narrative=NOTHING,
    )

    summary_id = write(org, llm)

    assert suggestions_of(org) == []
    assert summary_row(summary_id).merge_step["dropped"] == {
        "empty": 1,
        "outside": 1,
        "twice": 2,
        "reason": 0,
        "closed": 0,
        "already": 0,
    }


def test_an_issue_closed_while_it_was_asked_isn_t_suggested(
    two: tuple[Org_, str, str, str],
) -> None:
    org, low, high, _ = two

    def closing(payload: dict[str, Any]) -> dict[str, Any]:
        change(org, org.rep_id, high, now_of(high).version, status="resolved", note="Paid.")
        return suggesting({"into": low, "merge": [high], "reason": "Same payment."})

    summary_id = write(org, ByName(merge_suggestions=closing, summary_narrative=NOTHING))

    assert (suggestions_of(org), summary_row(summary_id).merge_step["dropped"]["closed"]) == ([], 1)


def test_a_dismissed_pair_is_never_suggested_again_either_way_round(
    two: tuple[Org_, str, str, str],
) -> None:
    org, low, high, _ = two
    write(
        org,
        ByName(
            merge_suggestions=suggesting({"into": low, "merge": [high], "reason": "Same."}),
            summary_narrative=NOTHING,
        ),
    )
    [(_, _, _, _)] = suggestions_of(org)
    with session_scope() as session:
        found = session.scalars(select(IssueMergeSuggestion.id)).one()
        merges.dismiss(session, org.org_id, org.rep_id, found)
    with session_scope() as session:  # dismissing again changes nothing
        assert merges.dismiss(session, org.org_id, org.rep_id, found).status == "dismissed"

    step = merges.suggest(
        str(asked_again(org)),
        ByName(merge_suggestions=suggesting({"into": high, "merge": [low], "reason": "Same."})),
        None,
        PACK,
        org.org_id,
        _money(org),
    )

    assert suggestions_of(org) == [(low, high, "dismissed", "Same.")]
    assert (step["kept"], step["dropped"]["already"]) == (0, 1)


def asked_again(org: Org_) -> str:
    """Last month's org summary: another summary to suggest from."""
    with session_scope() as session:
        return summary_jobs.ask(session, org.org_id, "org", None, "month").id


def _money(org: Org_) -> str:
    return team(org.org_id, "money")


def stored(org: Org_, low: str, high: str) -> str:
    """A suggestion to keep `low`, stored as the step stores one: its ID."""
    with session_scope() as session:
        found = IssueMergeSuggestion(
            org_id=org.org_id,
            team_id=_money(org),
            into_issue_id=low,
            merge_issue_id=high,
            pair_key=issues.pair_key(low, high),
            reason="Same.",
        )
        session.add(found)
        session.flush()
        return found.id


def accept(org: Org_, suggestion_id: str, low: str, high: str, pack_for: Any = lambda: PACK) -> str:
    with session_scope() as session:
        return merges.accept(
            session,
            pack_for,
            org.org_id,
            org.rep_id,
            suggestion_id,
            (now_of(low).version, now_of(high).version),
        ).id


def test_a_merge_locks_its_suggestion_before_anything_else(
    two: tuple[Org_, str, str, str],
) -> None:
    # Accept holds the suggestion while it merges: a merge from anywhere else takes it first too,
    # so the two can't deadlock.
    org, low, high, _ = two
    suggestion_id = stored(org, low, high)
    versions = ((low, now_of(low).version), (high, now_of(high).version))

    def merge_directly() -> None:
        with session_scope() as session:
            issues.merge(session, lambda: PACK, org.org_id, org.rep_id, *versions)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        session.execute(
            select(IssueMergeSuggestion.id)
            .where(IssueMergeSuggestion.id == suggestion_id)
            .with_for_update()
        )
        merging = pool.submit(merge_directly)
        wait_for(lambda: lock_waiters() == 1, "the merge never waited for its suggestion")
        with session_scope() as other:  # the issues aren't locked yet
            locked = other.execute(
                select(Issue.id).where(Issue.id.in_([low, high])).with_for_update(nowait=True)
            ).all()
        session.commit()

        merging.result(timeout=10)
    assert (len(locked), suggestions_of(org)) == (2, [(low, high, "merged", "Same.")])


def test_merging_again_once_the_kept_issue_is_merged_elsewhere_is_409(
    two: tuple[Org_, str, str, str],
) -> None:
    org, low, high, _ = two
    third = money_issue(
        visit_at(at_shop(org, "Third Shop", area="Third Area"), day(3), day(3), LOW)
    )
    suggestion_id = stored(org, low, high)
    versions = (now_of(low).version, now_of(high).version)
    accept(org, suggestion_id, low, high)
    with session_scope() as session:
        issues.merge(
            session,
            lambda: PACK,
            org.org_id,
            org.rep_id,
            (third, now_of(third).version),
            (low, now_of(low).version),
        )

    with pytest.raises(Conflict) as gone, session_scope() as session:
        merges.accept(session, lambda: PACK, org.org_id, org.rep_id, suggestion_id, versions)

    assert gone.value.code == "issue_merged"


def test_a_refused_merge_never_loads_the_pack(two: tuple[Org_, str, str, str]) -> None:
    org, low, high, _ = two
    suggestion_id = stored(org, low, high)
    with session_scope() as session:
        merges.dismiss(session, org.org_id, org.rep_id, suggestion_id)

    def no_pack() -> Any:
        raise AssertionError("the pack was loaded for a merge that doesn't happen")

    with pytest.raises(Conflict) as refused:
        accept(org, suggestion_id, low, high, pack_for=no_pack)
    other = stored(
        org, high, money_issue(visit_at(at_shop(org, "Third", "Third Area"), day(3), day(3), LOW))
    )
    with (
        pytest.raises(Conflict) as stale,
        session_scope() as session,
    ):  # refused in the merge itself
        merges.accept(session, no_pack, org.org_id, org.rep_id, other, (0, 0))

    assert (refused.value.code, stale.value.code) == ("suggestion_dismissed", "issue_changed")


def test_a_merge_marks_its_suggestion_merged_and_dismissing_it_then_is_409(
    two: tuple[Org_, str, str, str],
) -> None:
    org, low, high, _ = two
    write(
        org,
        ByName(
            merge_suggestions=suggesting({"into": low, "merge": [high], "reason": "Same."}),
            summary_narrative=NOTHING,
        ),
    )
    with session_scope() as session:
        suggestion_id = session.scalars(select(IssueMergeSuggestion.id)).one()
        issues.merge(
            session,
            lambda: PACK,
            org.org_id,
            org.rep_id,
            (low, now_of(low).version),
            (high, now_of(high).version),
        )

    with pytest.raises(Conflict) as merged, session_scope() as session:
        merges.dismiss(session, org.org_id, org.rep_id, suggestion_id)
    other = tiny_org("Their Org")
    with pytest.raises(NotFound) as theirs, session_scope() as session:
        merges.dismiss(session, other.org_id, other.rep_id, suggestion_id)

    assert suggestions_of(org) == [(low, high, "merged", "Same.")]
    assert (merged.value.code, theirs.value.code) == ("suggestion_merged", "suggestion_not_found")


def test_a_dropped_suggestion_doesn_t_make_another_twice_and_a_blank_reason_is_dropped(
    two: tuple[Org_, str, str, str],
) -> None:
    org, low, high, _ = two
    third = money_issue(
        visit_at(at_shop(org, "Third Shop", area="Third Area"), day(3), day(3), LOW)
    )
    answer = suggesting(
        {"into": low, "merge": [], "reason": "Nothing."},
        {"into": low, "merge": [high], "reason": "Same payment."},
        {"into": third, "merge": [str(uuid.uuid4())], "reason": "Not given."},
    )

    summary_id = write(org, ByName(merge_suggestions=answer, summary_narrative=NOTHING))
    blank = merges._checked(
        [
            {"into": low, "merge": [high], "reason": "   "},
            {"into": third, "merge": [low], "reason": "x" * 301},
        ],
        {low, high, third},
    )

    assert suggestions_of(org) == [(low, high, "open", "Same payment.")]
    assert summary_row(summary_id).merge_step["dropped"]["twice"] == 0
    assert blank == ([], Counter({"twice": 2}))
    assert merges._checked([{"into": low, "merge": [high], "reason": "   "}], {low, high}) == (
        [],
        Counter({"reason": 1}),
    )


def test_a_step_that_breaks_never_fails_the_summary(two: tuple[Org_, str, str, str]) -> None:
    org, _, _, _ = two

    def broken(payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("a bug in the step")

    summary_id = write(org, ByName(merge_suggestions=broken, summary_narrative=NOTHING))

    found = summary_row(summary_id)
    assert (found.status, found.merge_step) == ("ready", {"error": "unexpected_error"})


# --- once a summary, an extra that never fails it ----------------------------------------------


def test_a_failed_call_is_skipped_and_the_summary_still_written(
    two: tuple[Org_, str, str, str],
) -> None:
    org, _, _, _ = two
    busy = Outcome(
        calls=(call("llm_busy"),), error=RetryableError("llm_busy", "Muse Spark is busy")
    )

    summary_id = write(org, ByName(merge_suggestions=busy, summary_narrative=NOTHING))

    found = summary_row(summary_id)
    assert (found.status, found.merge_step["asked"], found.merge_step["failed"]) == ("ready", 1, 1)


def test_a_retried_job_doesn_t_ask_for_suggestions_again(two: tuple[Org_, str, str, str]) -> None:
    org, low, high, _ = two
    busy = Outcome(
        calls=(call("llm_busy"),), error=RetryableError("llm_busy", "Muse Spark is busy")
    )
    narratives = [busy, NOTHING]
    llm = ByName(
        merge_suggestions=suggesting({"into": low, "merge": [high], "reason": "Same."}),
        summary_narrative=lambda payload: narratives.pop(0),
    )
    summary_id = write(org, llm)
    assert jobs_of(summary_id) == ["queued"]  # the narrative's call failed: to be retried

    with session_scope(read_only=True) as session:
        make_due(session.scalars(select(Job.id).where(Job.summary_id == summary_id)).one())
    handlers = {"summary": worker.summarise_with(llm, packs=ORG_PACK_FIXTURES)}
    worker.Worker(WORKER_ID, handlers=handlers).run_once()

    assert [name for name, _ in llm.asked] == [
        "merge_suggestions",
        "summary_narrative",
        "summary_narrative",
    ]
    assert (summary_row(summary_id).status, len(suggestions_of(org))) == ("ready", 1)


# --- K6 ----------------------------------------------------------------------------------------


def test_k6_s_finance_gets_one_suggestion_e_s_issue_into_p1() -> None:
    world = _k6.load()
    p1 = k6_issue_of(world, world.insights["P1"][0], "finance")
    e = _e_issue(world)

    def p1_only(payload: dict[str, Any]) -> dict[str, Any]:
        ids = {i["id"] for i in payload["issues"]}
        found = [{"into": p1, "merge": [e], "reason": "The same Diwali payout."}]
        return suggesting(*found) if {p1, e} <= ids else suggesting()

    llm = ByName(merge_suggestions=p1_only, summary_narrative=NOTHING)
    with session_scope() as session:
        summary_id = summary_jobs.ask(
            session, world.org_id, "team", world.teams["finance"], "week", world.week(7).start
        ).id
    summary_jobs.run(summary_id, llm)

    with session_scope(read_only=True) as session:
        found = session.scalars(
            select(IssueMergeSuggestion).where(IssueMergeSuggestion.org_id == world.org_id)
        ).all()
    assert [(s.into_issue_id, s.merge_issue_id, s.status) for s in found] == [(p1, e, "open")]
    assert summary_row(summary_id).merge_step["kept"] == 1
    assert {name for name, _ in llm.asked} == {"merge_suggestions", "summary_narrative"}


def _e_issue(world: _k6.World) -> str:
    with session_scope(read_only=True) as session:
        e_insight = session.scalars(
            select(Insight.id)
            .join(Visit, Visit.id == Insight.visit_id)
            .where(Insight.id.in_(world.insights["P1"]), Visit.shop_id == world.shops["E"])
        ).one()
    return k6_issue_of(world, e_insight, "finance")
