"""K6's pattern check (batch 15c), for run_eval.py --patterns: the real merge step and narrative on
the K6 world, in the eval database. A module the script uses: it has no main.

The world is loaded into an org of its own each run, so runs never mix, and every team's
summary of K6's own W6 (26 Oct to 1 Nov 2026) is asked for: pinned, never "last week", so the
check reads the same whenever it runs. Without --yes: each summary's numbers counted and
checked, and its merge calls and narrative built as its job would, with an estimate: nothing is
sent, and no summary is written. With --yes: each team's summary job runs the worker's way (the
merge step, then the narrative). A job that can't be claimed, is left to retry (Meta not
reached, say) or is stopped by hand stops the run: no more is sent, and no results are written.
Every exit prints what was paid.

Reported, and kept in data/evals/results (run_eval's write_results):
- merge precision and recall against K6's one expected suggestion, E's issue into P1's: a pair
  suggested either way round counts, and whether it keeps P1's is said too. A team whose merge
  step didn't answer (an error, or a failed call) is listed, and if it's finance, recall is
  unknown (None), never 0;
- the narrative checker's drop rate: sentences dropped / (kept + dropped), and by why; the merge
  step's drops too;
- the cost, each summary's and in total.
It prints counts and costs, never the words.
"""

from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from countertalk import jobs, merges, narrative, summaries, summary_jobs, worker
from countertalk.db import session_scope
from countertalk.llm.base import StructuredLlm
from countertalk.models import IssueMergeSuggestion, JobKind, JobState, Summary, ist_date
from countertalk.playbooks import OrgPack
from scripts import _k6
from scripts._paid import (
    MERGE_ANSWER_TOKENS,
    NARRATIVE_ANSWER_TOKENS,
    Paid,
    estimate,
    paid_run,
    run_claimed,
    stop_code,
)

Packs = dict[tuple[str, int], OrgPack]
Write = Callable[[dict[str, Any], Packs, str], Path]
# The team whose merge K6 expects: E's issue into P1's.
EXPECTED_TEAM = "finance"


def check(yes: bool, llm: StructuredLlm | None, folders: dict[str, Path], write: Write) -> int:
    """The check (the module docstring), once run_eval has moved to the eval database."""
    world = _k6.load(packs=folders["packs"], playbooks=folders["playbooks"])
    period = world.week(6)
    print(f"K6 in org {world.org_id}: every team's summary of {period.kind} {_days(period)} (W6)")
    if not yes:
        return _dry_run(world, period)
    assert llm is not None
    asked = _ask(world)
    with paid_run(dict.fromkeys(asked.values(), 0), heading="Paid:", summary=True) as paid:
        states = _run(asked, llm, folders)
    if isinstance(states, int):
        return states
    results = _report(world, period, asked, paid)
    path = write(results, {(world.pack.id, world.pack.version): world.pack}, llm.model)
    print(f"\nResults: {path}")
    return 0 if all(state == JobState.DONE for state in states.values()) else 1


def _days(period: summaries.Period) -> str:
    """The period's first and last days, in IST."""
    return f"{ist_date(period.start)} to {ist_date(period.end) - timedelta(days=1)}"


def _dry_run(world: _k6.World, period: summaries.Period) -> int:
    total = 0.0
    for key in sorted(world.teams):
        team_id = world.teams[key]
        with session_scope(read_only=True) as session:
            numbers = summary_jobs.count(session, world.pack, world.org_id, team_id, period)
            problems = summaries.check(session, world.org_id, numbers)
            asks = merges.asks_for(session, world.pack, world.org_id, team_id)
            request = narrative.request_for(session, world.pack, world.org_id, numbers)
        costs = [estimate(a.messages, a.schema, MERGE_ANSWER_TOKENS) for a in asks]
        if request.offered:
            costs.append(estimate(request.messages, request.schema, NARRATIVE_ANSWER_TOKENS))
        usd = sum(c.usd for c in costs)
        total += usd
        problem = f"; numbers don't add up ({len(problems)})" if problems else ""
        print(
            f"{key}: {len(numbers.issues)} issues, {len(request.offered)} quotes,"
            f" {len(asks)} merge calls; about {sum(c.tokens_in for c in costs):,} input and"
            f" {sum(c.tokens_out for c in costs):,} output tokens, ${usd:.4f}{problem}"
        )
    print(f"in all: ${total:.4f} (an estimate: --patterns --yes sends it)")
    return 0


def _ask(world: _k6.World) -> dict[str, str]:
    """Each team's W6 summary, asked for: by team key."""
    asked = {}
    with session_scope() as session:
        for key in sorted(world.teams):
            found = summary_jobs.ask(
                session, world.org_id, "team", world.teams[key], "week", world.week(7).start
            )
            asked[key] = found.id
    return asked


def _run(
    asked: dict[str, str], llm: StructuredLlm, folders: dict[str, Path]
) -> dict[str, str] | int:
    """Each summary's job, the worker's way: each team's final job state, or an exit code once
    the run stops (_paid.stop_code). Inside paid_run, which prints what was paid (R8)."""
    runner = worker.Worker(handlers={JobKind.SUMMARY: worker.summarise_with(llm, **folders)})
    states: dict[str, str] = {}
    for key, summary_id in asked.items():
        with session_scope() as session:
            job = jobs.claim_for_summary(session, runner.id, summary_id)
        done = None if job is None else run_claimed(runner, job)
        code = stop_code(key, done)
        if code is not None:
            return code
        assert done is not None
        states[key] = done.state
    return states


def _unanswered(step: dict[str, Any] | None) -> str | None:
    """Why a team's merge step didn't fully answer, or None if it did."""
    if step is None:
        return "not run"
    if "error" in step:
        return str(step["error"])
    if step["failed"]:
        return f"{step['failed']} of {step['asked']} calls failed"
    return None


def _report(
    world: _k6.World, period: summaries.Period, asked: dict[str, str], paid: Paid
) -> dict[str, Any]:
    p1, e = _k6.expected_merge(world)
    with session_scope(read_only=True) as session:
        kept = session.scalars(
            select(IssueMergeSuggestion).where(IssueMergeSuggestion.org_id == world.org_id)
        ).all()
        rows = {
            s.id: s for s in session.scalars(select(Summary).where(Summary.id.in_(asked.values())))
        }
    expected = {(p1, e)}
    right = [s for s in kept if {s.into_issue_id, s.merge_issue_id} == {p1, e}]
    unanswered = {
        key: why
        for key, summary_id in asked.items()
        if (why := _unanswered(rows[summary_id].merge_step)) is not None
    }
    known = EXPECTED_TEAM not in unanswered
    narrative_dropped: Counter[str] = Counter()
    merge_dropped: Counter[str] = Counter()
    sentences_kept = 0
    for row in rows.values():
        if row.narrative is not None:
            sentences_kept += row.narrative["kept"]
            narrative_dropped.update(row.narrative["dropped"])
        if row.merge_step is not None and "dropped" in row.merge_step:
            merge_dropped.update(row.merge_step["dropped"])
    dropped = sum(narrative_dropped.values())
    cost = {key: paid.usd(summary_id) for key, summary_id in asked.items()}
    results = {
        "kind": "patterns",
        "period": f"W6, {_days(period)}",
        "merge": {
            "suggested": len(kept),
            "expected": len(expected),
            "right": len(right),
            "precision": len(right) / len(kept) if kept else None,
            # Unknown, not 0, when the expected pair's team got no full answer.
            "recall": len(right) / len(expected) if known else None,
            "keeps_p1": (bool(right) and right[0].into_issue_id == p1) if known else None,
            "unanswered": unanswered,
            "dropped": dict(merge_dropped),
        },
        "narrative": {
            "kept": sentences_kept,
            "dropped": dropped,
            "drop_rate": dropped / (sentences_kept + dropped) if sentences_kept + dropped else None,
            "dropped_by_why": dict(narrative_dropped),
        },
        "cost_usd": {"by_team": cost, "total": sum(cost.values())},
        "statuses": {key: rows[summary_id].status for key, summary_id in asked.items()},
    }
    merge, text = results["merge"], results["narrative"]
    print(
        f"merge: {merge['right']} of {merge['suggested']} suggested right, of {merge['expected']}"
        f" expected; keeps P1: {merge['keeps_p1']}; no full answer: {unanswered or 'none'}"
    )
    print(
        f"narrative: {text['kept']} sentences kept, {text['dropped']} dropped"
        f" {dict(narrative_dropped)}"
    )
    print(f"cost: ${results['cost_usd']['total']:.4f}")
    return results
