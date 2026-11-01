"""Extracts a transcribed visit with Muse Spark: a dev tool (batch 10).

    uv run python scripts/extract_visit.py <visit_id> [--yes] [--again]
        [--root <folder>] [--playbooks <folder>]

Without --yes it prints what would be sent: the turns, and an estimate of the tokens (reasoning
counted) and the cost at Muse Spark's prices. It sends nothing.

With --yes it queues the visit's extract job if it has none and claims it in the same
transaction, then runs it the worker's way: the same handler, heartbeat and failure handling,
with MODEL_API_KEY from backend/.env. Claiming the job is the lock: a worker or a second copy
can't take it meanwhile. Saved roles are reused.

--again extracts a visit in review (or failed) again, from its saved transcript and roles: only
extraction is paid for, so it refuses a visit whose roles aren't saved. Never after confirmed.

It prints counts, kinds, categories, severities and costs: never the words.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import func, select

from countertalk import extraction, llm_calls, roles
from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.llm.base import StructuredLlm
from countertalk.logs import configure_logging
from countertalk.models import (
    Evidence,
    Insight,
    Job,
    JobKind,
    JobState,
    Task,
    Team,
    Visit,
    VisitStatus,
    parse_id,
)
from countertalk.playbooks import ORG_PACKS_DIR, PLAYBOOKS_DIR
from scripts._paid import Stopped, estimate, model_llm, paid_run, run_extract_job

# A dry run's estimate of the answer's tokens: a summary, a few insights with quotes, a task or
# two. Reasoning is added on top (pricing).
ANSWER_TOKENS = 1_200
AGAIN_FROM = (VisitStatus.REVIEW, VisitStatus.FAILED)


def why_not(visit: Visit, again: bool, saved_roles: bool, job_states: list[str]) -> str | None:
    """Why --yes would send nothing for this visit, or None if it would."""
    if visit.status in AGAIN_FROM:
        if not again:
            return f"visit {visit.id} is {visit.status}: --again extracts it again"
        if not saved_roles:
            return f"visit {visit.id}'s roles aren't saved, and --again never pays for them"
        return None
    if visit.status == VisitStatus.TRANSCRIBED:
        return None
    if visit.status == VisitStatus.EXTRACTING and JobState.RUNNING not in job_states:
        return None
    jobs_now = ", ".join(job_states) or "no jobs"
    return f"nothing to send: visit {visit.id} is {visit.status} ({jobs_now})"


def dry_run(visit_id: str, saved: dict[str, tuple[str, float]], folders: dict[str, Path]) -> None:
    with session_scope(read_only=True) as session:
        org_id = session.get_one(Visit, visit_id).org_id
    pack = extraction.pack_for(org_id, **folders)
    request = extraction.request_for(visit_id, pack, {k: role for k, (role, _) in saved.items()})
    found = estimate(request.messages, request.schema, ANSWER_TOKENS)
    print(f"visit {visit_id}: {request.turns} turns, on {request.visit_date}")
    print(f"roles: {'saved, reused' if saved else 'to ask first (see assign_roles.py)'}")
    print(
        f"to send: {found.chars:,} characters;"
        f" about {found.tokens_in:,} input and {found.tokens_out:,} output tokens"
        f" (reasoning counted), ${found.usd:.4f} (an estimate: --yes sends it)"
    )


def show_result(visit_id: str) -> None:
    """Counts, kinds, categories and severities: never the words."""
    with session_scope(read_only=True) as session:
        visit = session.get_one(Visit, visit_id)
        teams = {team_id: key for key, team_id in session.execute(select(Team.key, Team.id))}
        found = session.scalars(
            select(Insight).where(Insight.visit_id == visit_id).order_by(Insight.seq)
        ).all()
        quotes = Counter(
            session.scalars(select(Evidence.insight_id).where(Evidence.visit_id == visit_id))
        )
        tasks = session.scalar(select(func.count()).where(Task.visit_id == visit_id))
        print(f"visit {visit_id}: {visit.status}, {len(found)} insights, {tasks} tasks")
        for row in found:
            team = teams.get(row.ai_team_id, "-") if row.ai_team_id else "-"
            print(
                f"  #{row.seq} {row.kind} {row.category} {row.severity}, team {team},"
                f" scheme {row.scheme_key or '-'}, {quotes[row.id]} quotes"
            )


def run(visit: Visit, again: bool, llm: StructuredLlm, folders: dict[str, Path]) -> int:
    with paid_run({visit.id: llm_calls.latest_seq(visit.id)}):
        done = run_extract_job(visit, again, llm, folders)
        if done is None:
            print(f"visit {visit.id} not extracted: it has no queued job", file=sys.stderr)
            return 1
        if done.stopped:
            print(f"stopped: the job is {done.state}", file=sys.stderr)
            return 130
        show_result(visit.id)
    if done.state == JobState.DONE:
        return 0
    # Queued again after a backoff: a worker would retry it, and pay again.
    print(f"job {done.state}: {done.last_error}", file=sys.stderr)
    with session_scope(read_only=True) as session:
        reason = session.get_one(Visit, visit.id).failure_reason
    if reason:
        print(f"visit failed: {reason}", file=sys.stderr)
    return 1


def main(argv: list[str], llm: StructuredLlm | None = None) -> int:
    parser = argparse.ArgumentParser(prog="extract_visit.py")
    parser.add_argument("visit_id")
    parser.add_argument("--yes", action="store_true", help="send it, and pay")
    parser.add_argument("--again", action="store_true", help="extract a visit in review again")
    parser.add_argument("--root", type=Path, default=ORG_PACKS_DIR, help="the org packs' folder")
    parser.add_argument(
        "--playbooks", type=Path, default=PLAYBOOKS_DIR, help="the playbooks' folder"
    )
    args = parser.parse_args(argv)
    visit_id = parse_id(args.visit_id)
    if visit_id is None:
        parser.error("that isn't a visit ID")
    folders = {"packs": args.root, "playbooks": args.playbooks}
    try:
        with session_scope(read_only=True) as session:
            visit = session.get(Visit, visit_id)
            if visit is None or visit.transcript_id is None:
                print(f"visit {visit_id} has no transcript to extract", file=sys.stderr)
                return 1
            saved = roles.saved_roles(session, visit.transcript_id)
            states = list(
                session.scalars(
                    select(Job.state).where(Job.visit_id == visit_id, Job.kind == JobKind.EXTRACT)
                )
            )
        if visit.status == VisitStatus.CONFIRMED and args.again:
            print(
                "failed: visit_confirmed: A confirmed visit can't be extracted again",
                file=sys.stderr,
            )
            return 1
        reason = why_not(visit, args.again, bool(saved), states)
        if reason is not None:
            print(reason, file=sys.stderr if args.yes else sys.stdout)
            return 1 if args.yes else 0
        if not args.yes:
            dry_run(visit_id, saved, folders)
            return 0
        llm = llm or model_llm()
        if llm is None:
            return 1
        return run(visit, args.again, llm, folders)
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1
    except DomainError as error:  # a pack that doesn't check out, or a visit it can't redo
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1
    except Stopped:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    configure_logging()  # the job's own lines: each call's tokens, never the prompt
    sys.exit(main(sys.argv[1:]))
