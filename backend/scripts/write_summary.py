"""Writes a team's or the org's summary with Muse Spark: a dev tool (batch 15b).

    uv run python scripts/write_summary.py <org_id> --team <key> --period week [--yes]
    uv run python scripts/write_summary.py <org_id> --org --period month [--yes]

The summary is of last week or last month, the latest whole one, as the summaries page asks.

Without --yes it counts the numbers and checks them, builds the narrative's request as the job
would, and prints how many issues and quotes it holds, and an estimate of the tokens (reasoning
counted) and the cost at Muse Spark's prices. It sends nothing and writes nothing.

With --yes it asks for the summary as POST /api/summaries does (a failed one is tried again, as
Try again does) and claims its job in the same transaction, then runs it the worker's way: the
same handler, heartbeat and failure handling, with MODEL_API_KEY from backend/.env. A summary
already written is never written, or paid for, again.

It prints counts and costs: never the words or the quotes.
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from countertalk import extraction, jobs, llm_calls, narrative, summaries, summary_jobs, worker
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.llm.base import StructuredLlm
from countertalk.logs import configure_logging
from countertalk.models import (
    JobKind,
    JobState,
    Org,
    Summary,
    SummaryStatus,
    Team,
    ist_date,
    parse_id,
    utc_now,
)
from countertalk.playbooks import ORG_PACKS_DIR, PLAYBOOKS_DIR
from countertalk.summaries import Period, PeriodKind
from scripts._paid import Stopped, estimate, model_llm, paid_run, run_claimed

# A dry run's estimate of the answer's tokens: an overview, and a sentence or two for each of
# up to ten issues, each citing an insight or two by its ID. Reasoning is added on top (pricing).
ANSWER_TOKENS = 1_500


def described(period: Period) -> str:
    last = ist_date(period.end - timedelta(days=1))
    return f"{period.kind} {ist_date(period.start).isoformat()} to {last.isoformat()}"


def dry_run(
    org_id: str, scope: str, team_id: str | None, period: Period, folders: dict[str, Path]
) -> int:
    pack = extraction.pack_for(org_id, **folders)
    with session_scope(read_only=True) as session:
        found = summary_jobs.existing(session, org_id, scope, team_id, period)
        numbers = summary_jobs.count(session, pack, org_id, team_id, period)
        problems = summaries.check(session, org_id, numbers)
        request = narrative.request_for(session, pack, org_id, numbers)
    totals = numbers.totals
    print(
        f"{scope} summary of the {described(period)}: {totals.insights} insights"
        f" at {totals.shops} shops"
    )
    if found is not None and found.status == SummaryStatus.READY:
        print(f"summary {found.id} is written already: --yes sends nothing")
        return 0
    if found is not None:
        print(f"summary {found.id} is {found.status}")
    for problem in problems:
        print(f"the numbers don't add up: {problem}")
    if problems:
        print("--yes would fail the summary, and pay nothing")
        return 1
    if not request.offered:
        print("nothing to cite: --yes would keep only its numbers, and pay nothing")
        return 0
    cost = estimate(request.messages, request.schema, ANSWER_TOKENS)
    print(
        f"to send: {len(request.sections)} issues, {len(request.offered)} quotes;"
        f" {cost.chars:,} characters; about {cost.tokens_in:,} input and {cost.tokens_out:,}"
        f" output tokens (reasoning counted), ${cost.usd:.4f} (an estimate: --yes sends it)"
    )
    return 0


def show_result(summary_id: str) -> None:
    """Its status, and how many sentences were kept and dropped: never the words."""
    with session_scope(read_only=True) as session:
        found = session.get_one(Summary, summary_id)
        written = found.narrative
        if found.status != SummaryStatus.READY:
            print(f"summary {summary_id}: {found.status}")
        elif written is None:
            print(f"summary {summary_id}: ready, only its numbers (nothing to cite)")
        else:
            # In DROPPED's order: JSONB keeps keys in its own.
            counts = written["dropped"]
            dropped = ", ".join(f"{why} {counts.get(why, 0)}" for why in narrative.DROPPED)
            print(
                f"summary {summary_id}: ready, {written['kept']} sentences kept by {found.model};"
                f" dropped: {dropped}"
            )


def run(
    org_id: str,
    scope: str,
    team_id: str | None,
    kind: PeriodKind,
    now: datetime,
    llm: StructuredLlm,
    folders: dict[str, Path],
) -> int:
    runner = worker.Worker(handlers={JobKind.SUMMARY: worker.summarise_with(llm, **folders)})
    with session_scope() as session:
        found = summary_jobs.ask(session, org_id, scope, team_id, kind, now)
        if found.status == SummaryStatus.READY:
            print(f"summary {found.id} is written already: nothing sent")
            return 0
        if found.status == SummaryStatus.FAILED:
            summary_jobs.retry(session, org_id, found.id)
        summary_id = found.id
        # Where this run's calls start, read before the claim: nothing between the claim and
        # paid_run can then fail and leave the job running.
        before = llm_calls.latest_seq(summary_id, summary=True)
        job = jobs.claim_for_summary(session, runner.id, summary_id)
    if job is None:
        print(f"summary {summary_id} not written: its job is running elsewhere", file=sys.stderr)
        return 1
    with paid_run({summary_id: before}, summary=True):
        done = run_claimed(runner, job)
        if done.stopped:
            print(f"stopped: the job is {done.state}", file=sys.stderr)
            return 130
        show_result(summary_id)
    if done.state == JobState.DONE:
        return 0
    # Queued again after a backoff: a worker would retry it, and pay again.
    print(f"job {done.state}: {done.last_error}", file=sys.stderr)
    with session_scope(read_only=True) as session:
        reason = session.get_one(Summary, summary_id).failure_reason
    if reason:
        print(f"summary failed: {reason}", file=sys.stderr)
    return 1


def main(argv: list[str], llm: StructuredLlm | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="write_summary.py")
    parser.add_argument("org_id")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--team", help="a team's summary, by the team's key")
    which.add_argument("--org", action="store_true", help="the org's (leadership) summary")
    parser.add_argument("--period", choices=["week", "month"], required=True)
    parser.add_argument("--yes", action="store_true", help="send it, and pay")
    parser.add_argument("--root", type=Path, default=ORG_PACKS_DIR, help="the org packs' folder")
    parser.add_argument(
        "--playbooks", type=Path, default=PLAYBOOKS_DIR, help="the playbooks' folder"
    )
    args = parser.parse_args(argv)
    org_id = parse_id(args.org_id)
    if org_id is None:
        parser.error("that isn't an org ID")
    folders = {"packs": args.root, "playbooks": args.playbooks}
    scope = "org" if args.org else "team"
    now = now or utc_now()
    try:
        with session_scope(read_only=True) as session:
            if session.get(Org, org_id) is None:
                print(f"org {org_id} doesn't exist", file=sys.stderr)
                return 1
            team_id = None
            if args.team is not None:
                team_id = session.scalars(
                    select(Team.id).where(Team.org_id == org_id, Team.key == args.team)
                ).one_or_none()
                if team_id is None:
                    print(f"org {org_id} has no team {args.team}", file=sys.stderr)
                    return 1
        if not args.yes:
            return dry_run(
                org_id, scope, team_id, summaries.last_complete(args.period, now), folders
            )
        llm = llm or model_llm()
        if llm is None:
            return 1
        return run(org_id, scope, team_id, args.period, now, llm, folders)
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:  # a pack that doesn't check out
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1
    except Stopped:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    configure_logging()  # the job's own lines: counts and each call's tokens, never the words
    sys.exit(main(sys.argv[1:]))
