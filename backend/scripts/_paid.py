"""What the paid dev scripts share (CODING_GUIDELINES §8.5 R8): the key, the dry run's estimate,
running the extract job the worker's way, and on every exit what was paid, counting only the
calls actually sent. A module, not a script (the leading underscore): it has no main.
"""

import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from countertalk import jobs, llm_calls, reprocess, visits, worker
from countertalk.config import get_settings
from countertalk.db import SETUP_ERRORS, session_scope
from countertalk.errors import RetryableError
from countertalk.llm import pricing
from countertalk.llm.base import CallDetails, Message, StructuredLlm
from countertalk.llm.client import MetaLlm
from countertalk.models import Job, JobKind, JobState, LlmPurpose, Visit, VisitStatus

# A dry run's estimate of a summary's answers (batch 15b, 15c), reasoning added on top:
# the narrative, an overview and a sentence or two for each of up to ten issues, each citing
# an insight or two by its ID; a merge suggestion call, a few pairs at most, each with a reason.
NARRATIVE_ANSWER_TOKENS = 1_500
MERGE_ANSWER_TOKENS = 400


def model_llm() -> StructuredLlm | None:
    """Muse Spark with MODEL_API_KEY from backend/.env, or None once it's said the key isn't
    set. Asked before any job is claimed, so a run without a key uses up none of its tries."""
    key = get_settings().MODEL_API_KEY.get_secret_value()
    if not key:
        print("MODEL_API_KEY isn't set in backend/.env", file=sys.stderr)
        return None
    return MetaLlm(key)


@dataclass(frozen=True)
class Estimate:
    chars: int
    tokens_in: int
    # Reasoning counted (pricing).
    tokens_out: int
    usd: float


def estimate(messages: Sequence[Message], schema: dict[str, Any], answer_tokens: int) -> Estimate:
    tokens_in = pricing.input_tokens(messages, schema)
    tokens_out = pricing.output_tokens(answer_tokens)
    chars = pricing.request_chars(messages, schema)
    return Estimate(chars, tokens_in, tokens_out, pricing.cost(tokens_in, 0, tokens_out))


def show_calls(calls: Sequence[CallDetails], purposes: Sequence[str] | None = None) -> None:
    """Each call's tokens, time and cost, then the total. With `purposes`, each call's too."""
    for number, call in enumerate(calls, start=1):
        what = f"{purposes[number - 1]} " if purposes else ""
        print(
            f"call {number}: {what}{call.error or 'ok'}, {call.input_tokens or 0:,} input"
            f" ({call.cached_input_tokens or 0:,} cached), {call.output_tokens or 0:,} output"
            f" ({call.reasoning_tokens or 0:,} reasoning), {call.latency_ms / 1000:.1f} s,"
            f" ${pricing.call_cost(call):.4f}"
        )
    print(f"cost: ${sum(map(pricing.call_cost, calls)):.4f}")


def paid_since(visit_id: str, seq: int, purpose: LlmPurpose | None = None) -> list[CallDetails]:
    return [call for _, call in llm_calls.since(visit_id, seq, purpose)]


class Stopped(Exception):
    """Ctrl+C during a paid run, once what was paid is printed: exit 130."""


@dataclass
class Paid:
    """What a paid run's visits logged, filled in on its way out: {visit: [(purpose, call)]}."""

    calls: dict[str, list[tuple[str, CallDetails]]] = field(default_factory=dict)

    def usd(self, visit_id: str) -> float:
        return sum(pricing.call_cost(call) for _, call in self.calls.get(visit_id, []))


@contextmanager
def paid_run(
    since: dict[str, int], heading: str | None = None, *, summary: bool = False
) -> Iterator[Paid]:
    """Around a paid run (the R8 guard): on every way out, done, failed or stopped, it prints
    each call the visits (or, with `summary`, the summaries) logged after their seq in
    `since`, and the cost. Ctrl+C anywhere inside becomes Stopped. If the database doesn't
    answer, it says so in one line: llm_calls has the calls."""
    paid = Paid()
    try:
        yield paid
    except KeyboardInterrupt:
        raise Stopped from None
    finally:
        try:
            paid.calls = {
                key: llm_calls.since(key, seq, summary=summary) for key, seq in since.items()
            }
        except SETUP_ERRORS:
            print(
                "what was paid can't be read: the database didn't answer (llm_calls has it)",
                file=sys.stderr,
            )
        else:
            every = [call for calls in paid.calls.values() for call in calls]
            if heading:
                print(heading)
            show_calls([call for _, call in every], [purpose for purpose, _ in every])


@dataclass(frozen=True)
class JobRun:
    state: str
    last_error: str | None
    # Stopped by hand (Ctrl+C): the job was failed, to be retried.
    stopped: bool = False


def run_extract_job(
    visit: Visit, again: bool, llm: StructuredLlm, folders: dict[str, Path]
) -> JobRun | None:
    """The visit's extract job, queued (again, for --again) and claimed in one transaction so
    no worker takes it in between, then run the worker's way: the same handler, heartbeat and
    failure handling. None if there was no job to claim. A job left queued failed in a way a
    retry might fix, such as not reaching Meta: the caller sends no more."""
    runner = worker.Worker(handlers={JobKind.EXTRACT: worker.extract_with(llm, **folders)})
    with session_scope() as session:
        if again:
            reprocess.reprocess(session, visit.org_id, visit.id, "extract")
        elif visit.status == VisitStatus.TRANSCRIBED:
            jobs.enqueue(session, JobKind.EXTRACT, visit_id=visit.id)
        job = jobs.claim_for_visit(session, runner.id, visit.id, JobKind.EXTRACT)
    if job is None:
        return None
    return run_claimed(runner, job)


def run_claimed(runner: worker.Worker, job: jobs.ClaimedJob) -> JobRun:
    """A job claimed by hand, run the worker's way. Ctrl+C fails it, to be retried: the job
    isn't left running, and the caller sends no more."""
    try:
        runner.run_job(job)
    except KeyboardInterrupt:
        stopped = RetryableError("stopped_by_hand", "The run was stopped part-way")
        with session_scope() as session:
            jobs.mark_failed(session, job, runner.id, stopped, visits.fail_processing)
        return JobRun(*job_state(job.id), stopped=True)
    return JobRun(*job_state(job.id))


def job_state(job_id: str) -> tuple[str, str | None]:
    with session_scope(read_only=True) as session:
        state, last_error = session.execute(
            select(Job.state, Job.last_error).where(Job.id == job_id)
        ).one()
        return state, last_error


def stop_code(name: str, done: JobRun | None) -> int | None:
    """After each job of a paid run of several (run_eval's cases, the pattern check's teams):
    None to go on, or the run's exit code, said in one line, once no more is sent: no job to
    claim (1), stopped by hand (130) or left to retry (1). A failed job goes on: the results
    show it. The caller writes no results for a run it stops: they'd score what never ran."""
    if done is None:
        print(f"{name}: its job couldn't be claimed, so no more is sent", file=sys.stderr)
        return 1
    if done.stopped:
        print(f"{name}: stopped, so no more is sent", file=sys.stderr)
        return 130
    if done.state == JobState.QUEUED:
        print(f"{name}: {done.last_error}; no more is sent", file=sys.stderr)
        return 1
    if done.state != JobState.DONE:
        print(f"{name}: failed: {done.last_error}", file=sys.stderr)
    return None
