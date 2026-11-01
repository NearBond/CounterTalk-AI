"""Reprocesses the visits that failed for one reason, once its service is fixed (batch 13): a
dev tool.

    uv run python scripts/reprocess_failed.py --code <error code> --since <when> [--yes]

It finds failed visits whose newest job died with that code (llm_auth_failed, llm_unavailable,
stt_auth_failed, …) at or after --since: a date (midnight IST) or a time with its zone, like
2026-11-01 or 2026-11-01T18:30:00+05:30. Nothing else is touched (§9.7).

Without --yes it lists them, and the step each would run again from: the step whose job
died. With --yes it reprocesses each, in a transaction of its own, through the API's own
reprocess():
- from extract: only extraction is paid for again;
- from transcribe: speech-to-text is paid for, but replies the failed run saved are reused
  (R8).
A worker then runs the jobs: this script calls no provider.

A job that died before batch 13 has no stored code, so --code never matches it: how many such
failed visits there are since --since is shown, never folded in (R10).
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import func, or_, select

from countertalk.db import SETUP_ERRORS, session_scope, setup_trouble
from countertalk.errors import DomainError, describe
from countertalk.logs import configure_logging
from countertalk.models import IST, Job, JobKind, JobState, Visit, VisitStatus
from countertalk.playbooks import ISO_DATE
from countertalk.reprocess import From, reprocess


@dataclass(frozen=True)
class Failed:
    visit_id: str
    org_id: str
    start: From
    failed_at: datetime


def since_of(text: str) -> datetime:
    """A date (midnight IST) or a time with its zone (R4): ValueError otherwise."""
    if ISO_DATE.fullmatch(text):
        return datetime.combine(date.fromisoformat(text), time(), IST)
    found = datetime.fromisoformat(text)
    if found.tzinfo is None:
        raise ValueError("a time needs its zone")
    return found


def failed_since(code: str, since: datetime) -> tuple[list[Failed], int]:
    """The failed visits whose newest job died with this code since then, oldest first, and how
    many died since then with no code stored (before batch 13)."""
    newest = (
        select(Job.visit_id, func.max(Job.seq).label("seq"))
        .where(Job.visit_id.is_not(None))
        .group_by(Job.visit_id)
        .subquery()
    )
    query = (
        select(Visit.id, Visit.org_id, Job.kind, Job.last_error_code, Job.updated_at)
        .join(newest, newest.c.visit_id == Visit.id)
        .join(Job, Job.seq == newest.c.seq)
        .where(
            Visit.status == VisitStatus.FAILED,
            Job.state == JobState.DEAD,
            Job.updated_at >= since,
            or_(Job.last_error_code == code, Job.last_error_code.is_(None)),
        )
        .order_by(Job.updated_at, Visit.seq)
    )
    with session_scope(read_only=True) as session:
        rows = session.execute(query).all()
    found = [
        Failed(visit_id, org_id, "transcribe" if kind == JobKind.TRANSCRIBE else "extract", at)
        for visit_id, org_id, kind, job_code, at in rows
        if job_code == code
    ]
    return found, sum(1 for row in rows if row.last_error_code is None)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="reprocess_failed.py")
    parser.add_argument("--code", required=True, help="the error code the visits failed with")
    parser.add_argument("--since", required=True, help="2026-11-01, or a time with its zone")
    parser.add_argument("--yes", action="store_true", help="reprocess them")
    args = parser.parse_args(argv)
    try:
        since = since_of(args.since)
    except ValueError:
        parser.error("--since is a date like 2026-11-01, or a time with its zone")
    try:
        found, uncoded = failed_since(args.code, since)
        print(f"{len(found)} failed visits with {args.code} since {since.isoformat()}")
        for failed in found:
            print(f"  {failed.visit_id} failed {failed.failed_at.isoformat()}, from {failed.start}")
        if uncoded:
            print(f"  not matched: {uncoded} failed before batch 13, with no code stored")
        if not args.yes:
            if any(failed.start == "transcribe" for failed in found):
                print("from transcribe: speech-to-text is paid for (saved replies reused)")
            print("--yes reprocesses them")
            return 0
        refused = 0
        for failed in found:
            try:
                with session_scope() as session:
                    reprocess(session, failed.org_id, failed.visit_id, failed.start)
                print(f"  {failed.visit_id}: queued from {failed.start}")
            except DomainError as error:  # changed meanwhile: confirmed, or already running
                refused += 1
                print(f"  {failed.visit_id}: not reprocessed: {describe(error)}", file=sys.stderr)
        return 1 if refused else 0
    except SETUP_ERRORS as error:
        print(f"failed: {setup_trouble(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
