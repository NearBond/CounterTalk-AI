"""Transcribes a stored visit with Muse Voice Transcribe: a dev tool (batch 6).

    uv run python scripts/transcribe_visit.py <visit_id> [--no-language-bias] [--yes]

Without --yes it prepares the audio (free) and prints each chunk, whether a saved reply will be
reused, and the seconds and cost of what would be sent. It sends nothing.

With --yes it runs the visit's own queued transcribe job through the worker's path, the same
handler, heartbeat and failure handling as a worker, with MODEL_API_KEY from backend/.env. It
reuses saved replies. Claiming that job is the lock: a second copy, or a worker, can't take it
while it runs, and once done there's no queued job left to transcribe the visit again.

It prints sizes, times, counts and costs: never the recording's words.
"""

import argparse
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from countertalk import audio, jobs, transcription, visits, worker
from countertalk.config import get_settings
from countertalk.db import session_scope
from countertalk.errors import DomainError, RetryableError, describe
from countertalk.logs import configure_logging
from countertalk.models import (
    Job,
    JobKind,
    JobState,
    Segment,
    Turn,
    Visit,
    VisitStatus,
    parse_id,
)
from countertalk.stt.base import SpeechToText
from countertalk.stt.meta import MetaSpeechToText

USD_PER_AUDIO_HOUR = 0.18  # Muse Voice Transcribe (BRIEF §8)


def cost(seconds: float) -> str:
    return f"${seconds * USD_PER_AUDIO_HOUR / 3600:.4f}"


def job_states(session: Session, visit_id: str) -> list[str]:
    query = select(Job.state).where(Job.visit_id == visit_id, Job.kind == JobKind.TRANSCRIBE)
    return list(session.scalars(query.order_by(Job.seq)))


def hint_for(visit_id: str) -> tuple[str, ...]:
    """The hint a worker would send: its saved replies', or the org pack's (batch T3)."""
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        org_id = None if visit is None else visit.org_id
    # No such visit: the run says so, whatever the hint.
    if org_id is None:
        return transcription.LANGUAGES
    return tuple(transcription.visit_languages(visit_id, org_id))


def dry_run(visit_id: str, stt: SpeechToText, languages: tuple[str, ...]) -> int:
    # Read in one short transaction, then prepare with none open (§2.4).
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        if visit is None or visit.completed_at is None or visit.source_ext is None:
            print(f"visit {visit_id} isn't a completed upload", file=sys.stderr)
            return 1
        status, ext, states = visit.status, visit.source_ext, job_states(session, visit_id)
        org_id = visit.org_id
        seqs = list(
            session.scalars(
                select(Segment.seq).where(Segment.visit_id == visit_id).order_by(Segment.seq)
            )
        )
    waiting = status in (VisitStatus.UPLOADED, VisitStatus.TRANSCRIBING)
    if not waiting or JobState.QUEUED not in states:
        # What --yes would do: nothing. And no preparing under a job that's running.
        print(f"nothing to send: visit {visit_id} is {status}, jobs: {', '.join(states) or 'none'}")
        return 0
    prepared = audio.prepare(visit_id, ext, seqs)
    keywords = transcription.visit_keywords(visit_id, org_id)
    print(f"visit {visit_id}: {status}, {prepared.duration_ms / 1000:.1f} s of audio")
    print(f"languageBias: {', '.join(languages) or 'none'}")
    shown = ", ".join(keywords[:5]) + (", ..." if len(keywords) > 5 else "")
    print(f"keywords: {len(keywords)}" + (f" ({shown})" if keywords else ""))
    to_send = 0.0
    for chunk in prepared.chunks:
        seconds = (chunk.end_ms - chunk.start_ms) / 1000
        saved = transcription.saved_reply(visit_id, chunk, stt, list(languages), keywords)
        what = "saved reply, reused" if saved is not None else "to send"
        print(f"chunk {chunk.index}: {what} ({seconds:.1f} s, {chunk.bytes:,} bytes)")
        if saved is None:
            to_send += seconds
    print(f"to send: {to_send:.1f} s, about {cost(to_send)} at ${USD_PER_AUDIO_HOUR} an hour")
    return 0


def run(visit_id: str, stt: SpeechToText, languages: tuple[str, ...] | None) -> int:
    """The job, the worker's way: with `languages` None, the hint is worked out as the
    worker does, after a redo has removed the replies it replaces."""
    runner = worker.Worker(handlers={JobKind.TRANSCRIBE: worker.transcribe_with(stt, languages)})
    with session_scope() as session:
        job = jobs.claim_for_visit(session, runner.id, visit_id, JobKind.TRANSCRIBE)
    if job is None:
        with session_scope(read_only=True) as session:
            states = job_states(session, visit_id)
        why = "it's already running" if JobState.RUNNING in states else "it has no queued job"
        print(
            f"visit {visit_id} not transcribed: {why} ({', '.join(states) or 'no jobs'})",
            file=sys.stderr,
        )
        return 1
    try:
        runner.run_job(job)
    except KeyboardInterrupt:
        # Ctrl+C: queued again (after a backoff) rather than left running until it goes stale.
        # A reply in flight is lost; every saved one is kept.
        stopped = RetryableError("stopped_by_hand", "The run was stopped part-way")
        with session_scope() as session:
            jobs.mark_failed(session, job, runner.id, stopped, visits.fail_processing)
        print("stopped: the job is queued again, and the saved replies are kept", file=sys.stderr)
        return 130
    with session_scope(read_only=True) as session:
        visit = session.get(Visit, visit_id)
        state, last_error = session.execute(
            select(Job.state, Job.last_error).where(Job.id == job.id)
        ).one()
        turns = 0
        if visit is not None and visit.transcript_id is not None:
            turns = session.scalar(
                select(func.count()).where(Turn.transcript_id == visit.transcript_id)
            )
    assert visit is not None
    billed = visit.stt_seconds_billed
    print(
        f"billed so far: {billed} s ({cost(billed)}), calls took {visit.stt_wall_ms / 1000:.1f} s"
    )
    if state == JobState.DONE:
        print(f"transcribed: {turns} turns")
        return 0
    print(f"job {state}: {last_error}", file=sys.stderr)
    if visit.failure_reason:
        print(f"visit failed: {visit.failure_reason}", file=sys.stderr)
    return 1


def main(argv: list[str], stt: SpeechToText | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transcribe_visit.py")
    parser.add_argument("visit_id")
    parser.add_argument("--no-language-bias", action="store_true")
    parser.add_argument("--yes", action="store_true", help="send it, and pay")
    args = parser.parse_args(argv)
    visit_id = parse_id(args.visit_id)
    if visit_id is None:
        parser.error("that isn't a visit ID")
    if stt is None:
        key = get_settings().MODEL_API_KEY.get_secret_value()
        if args.yes and not key:
            # Before the claim, so a run without a key doesn't use up one of the job's attempts.
            print("MODEL_API_KEY isn't set in backend/.env", file=sys.stderr)
            return 1
        stt = MetaSpeechToText(key)
    try:
        languages = () if args.no_language_bias else hint_for(visit_id)
        if not args.yes:
            return dry_run(visit_id, stt, languages)
        return run(visit_id, stt, () if args.no_language_bias else None)
    except DomainError as error:
        print(f"failed: {describe(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    configure_logging()  # the job's own lines: each call, its session and its time
    sys.exit(main(sys.argv[1:]))
