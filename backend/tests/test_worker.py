"""worker.py: the loop, handlers, the heartbeat, housekeeping and stopping (batch 4)."""

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from countertalk import jobs, limits, visits, worker
from countertalk.config import BACKEND_DIR
from countertalk.db import session_scope
from countertalk.errors import PermanentError, RetryableError
from countertalk.models import Job, JobKind, JobState, VisitStatus
from tests.conftest import PINNED_SETTINGS
from tests.test_jobs import OTHER_WORKER, claimed, job_row, make_stale, queue, queued_for
from tests.test_storage import OLD_MS, file_aged
from tests.test_visits import Org_, load, make_org, new_visit, quiet_for, visit_at

WORKER_ID = "test-host:1:cccccc"
RUN_TIMEOUT_S = 30


@pytest.fixture
def org() -> Org_:
    return make_org()


def raising(error: BaseException) -> worker.Handler:
    def handler(ctx: worker.JobContext) -> None:
        raise error

    return handler


def old_staged_file(data_dir: Path) -> Path:
    return file_aged(data_dir / "tmp" / "left-by-a-crash.part", OLD_MS)


def quiet_visit(org: Org_) -> str:
    """A browser visit still recording, with nothing new for longer than the abandoned limit."""
    visit_id = new_visit(org)
    quiet_for(visit_id, limits.ABANDONED_UPLOAD_MS + 60_000)
    return visit_id


# --- the loop and handlers --------------------------------------------------------------------


def test_a_turn_runs_the_oldest_job_and_marks_it_done() -> None:
    ran: list[str] = []
    job_id = queue()

    assert worker.Worker(
        WORKER_ID, handlers={JobKind.PING: lambda ctx: ran.append(ctx.job.id)}
    ).run_once()

    assert ran == [job_id]
    assert job_row(job_id).state == JobState.DONE


def test_with_nothing_due_a_turn_runs_nothing() -> None:
    assert worker.Worker(WORKER_ID).run_once() is False


def test_ping_and_sleep_are_the_test_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "SLEEP_JOB_MS", 10)
    ids = [queue(JobKind.PING), queue(JobKind.SLEEP)]
    turn = worker.Worker(WORKER_ID)

    assert turn.run_once() and turn.run_once()

    assert [job_row(job_id).state for job_id in ids] == [JobState.DONE, JobState.DONE]


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (RetryableError("stt_busy", "The speech service is busy."), JobState.QUEUED),
        (PermanentError("unreadable_audio", "The recording can't be read."), JobState.DEAD),
        (ValueError("someone@example.com"), JobState.QUEUED),
    ],
)
def test_a_handlers_error_is_retried_or_kills_the_job(
    error: BaseException, state: str, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = queue()

    with caplog.at_level(logging.DEBUG):
        worker.Worker(WORKER_ID, handlers={JobKind.PING: raising(error)}).run_once()

    assert job_row(job_id).state == state
    assert "someone" not in caplog.text


def test_a_job_that_dies_in_the_worker_fails_its_visit(org: Org_) -> None:
    visit_id = visit_at(org, VisitStatus.TRANSCRIBING)
    queued_for(visit_id)
    unreadable = PermanentError("unreadable_audio", "The recording can't be read.")

    worker.Worker(WORKER_ID, handlers={JobKind.TRANSCRIBE: raising(unreadable)}).run_once()

    visit = load(visit_id)
    assert (visit.status, visit.failure_reason) == ("failed", "The recording can't be read.")


def test_housekeeping_fails_the_visit_of_a_job_stale_on_its_last_try(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(jobs.KINDS, JobKind.TRANSCRIBE, 1)
    visit_id = visit_at(org, VisitStatus.TRANSCRIBING)
    queued_for(visit_id)
    make_stale(claimed(OTHER_WORKER).id)

    worker.Worker(WORKER_ID).housekeep()

    assert load(visit_id).failure_reason == jobs.interrupted(1)


def test_a_kind_with_no_handler_is_left_for_another_worker() -> None:
    # During an overlapping deploy, an old worker can claim a kind only the new one knows: it
    # retries later instead of failing the rep's visit, and still dies after its attempts.
    job_id = queue()

    worker.Worker(WORKER_ID, handlers={}).run_once()

    row = job_row(job_id)
    assert row.state == JobState.QUEUED
    assert row.last_error is not None
    assert row.last_error.startswith("RetryableError: unknown_job_kind")


def test_every_job_log_line_names_the_job(caplog: pytest.LogCaptureFixture) -> None:
    job_id = queue()

    with caplog.at_level(logging.INFO):
        worker.Worker(WORKER_ID).run_once()

    lines = [r.getMessage() for r in caplog.records if f"job={job_id}" in r.getMessage()]
    assert len(lines) >= 3  # started, the ping itself, done
    for line in lines:
        assert "kind=ping" in line
        assert "visit=None" in line
        assert "attempt=" in line


def test_each_worker_has_its_own_id_naming_its_host_and_process() -> None:
    first, second = worker.new_worker_id(), worker.new_worker_id()

    host, pid, _ = first.rsplit(":", 2)
    assert (host, pid) == (socket.gethostname(), str(os.getpid()))
    assert first != second


# --- §9.5: the loop survives any single error ---------------------------------------------------


def test_an_error_while_claiming_doesnt_stop_the_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_claim = jobs.claim
    calls: list[int] = []

    def flaky_claim(session: object, worker_id: str) -> jobs.ClaimedJob | None:
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("UPDATE jobs ...", None, Exception("lock timeout"))
        return real_claim(session, worker_id)  # type: ignore[arg-type]

    monkeypatch.setattr(jobs, "claim", flaky_claim)
    job_id = queue()
    turn = worker.Worker(WORKER_ID)

    assert turn.run_once() is False
    assert turn.run_once() is True
    assert job_row(job_id).state == JobState.DONE
    assert "claim failed" in caplog.text


def test_an_error_while_marking_a_job_leaves_it_for_recovery(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*args: object) -> bool:
        raise OperationalError("UPDATE jobs ...", None, Exception("connection lost"))

    monkeypatch.setattr(jobs, "mark_done", broken)
    job_id = queue()

    assert worker.Worker(WORKER_ID).run_once() is True

    assert job_row(job_id).state == JobState.RUNNING  # stale in time, then recovered (§9.1)
    assert f"job={job_id}" in caplog.text


def test_the_loop_carries_on_after_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "WORKER_IDLE_MS", 10)
    turns: list[int] = []
    the_worker = worker.Worker(WORKER_ID)

    def failing_turn() -> bool:
        turns.append(1)
        if len(turns) == 3:
            the_worker.request_stop(signal.SIGTERM)
        raise OperationalError("SELECT 1", None, Exception("down"))

    monkeypatch.setattr(the_worker, "run_once", failing_turn)

    the_worker.run()

    assert len(turns) == 3


# --- §9.2: the heartbeat -----------------------------------------------------------------------


def test_the_heartbeat_keeps_a_long_job_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "JOB_HEARTBEAT_MS", 50)
    monkeypatch.setattr(limits, "STALE_JOB_MS", 400)
    found: list[list[jobs.Recovered]] = []

    def long_job(ctx: worker.JobContext) -> None:
        time.sleep(3 * limits.STALE_JOB_MS / 1000)
        found.append(jobs.recover_stale(visits.fail_processing))  # another worker, mid-job

    job_id = queue()

    worker.Worker(WORKER_ID, handlers={JobKind.PING: long_job}).run_once()

    assert found == [[]]
    assert job_row(job_id).state == JobState.DONE


def test_the_heartbeat_stops_with_its_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "JOB_HEARTBEAT_MS", 20)
    queue()

    worker.Worker(WORKER_ID, handlers={JobKind.PING: lambda ctx: time.sleep(0.1)}).run_once()

    assert not [t for t in threading.enumerate() if t.name.startswith("heartbeat")]


def test_a_heartbeat_that_finds_its_claim_lost_stops_beating(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(limits, "JOB_HEARTBEAT_MS", 20)
    beats: list[bool] = []
    real_heartbeat = jobs.heartbeat

    def counted(*args: object) -> bool:
        beats.append(real_heartbeat(*args))  # type: ignore[arg-type]
        return beats[-1]

    monkeypatch.setattr(jobs, "heartbeat", counted)

    told: list[bool] = []

    def lose_the_claim(ctx: worker.JobContext) -> None:
        with session_scope() as session:
            session.execute(update(Job).where(Job.id == ctx.job.id).values(locked_by=OTHER_WORKER))
        time.sleep(0.3)
        told.append(ctx.claim_lost.is_set())  # what a paid handler checks before each call

    queue()
    worker.Worker(WORKER_ID, handlers={JobKind.PING: lose_the_claim}).run_once()

    assert beats.count(False) == 1  # it stopped at the first lost beat
    assert told == [True]
    assert "claim lost" in caplog.text


def test_a_claim_is_taken_as_lost_once_heartbeats_have_failed_for_the_stale_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If beats keep failing, another worker may already have recovered the job: a paid handler
    # must stop then too, not only when a beat finds the claim gone.
    monkeypatch.setattr(limits, "JOB_HEARTBEAT_MS", 20)
    monkeypatch.setattr(limits, "STALE_JOB_MS", 150)

    def broken(*args: object) -> bool:
        raise OperationalError("UPDATE jobs ...", None, Exception("connection lost"))

    monkeypatch.setattr(jobs, "heartbeat", broken)
    told: list[bool] = []

    def slow(ctx: worker.JobContext) -> None:
        told.append(ctx.claim_lost.is_set())
        time.sleep(0.4)
        told.append(ctx.claim_lost.is_set())

    queue()
    worker.Worker(WORKER_ID, handlers={JobKind.PING: slow}).run_once()

    assert told == [False, True]


# --- housekeeping ------------------------------------------------------------------------------


def test_housekeeping_runs_at_start_and_then_every_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[int] = []
    monkeypatch.setattr(jobs, "recover_stale", lambda fail_visit: runs.append(1) or [])
    now = [1_000.0]
    turn = worker.Worker(WORKER_ID, clock=lambda: now[0])

    turn.run_once()
    turn.run_once()
    assert runs == [1]

    now[0] += limits.HOUSEKEEPING_INTERVAL_MS / 1000
    turn.run_once()
    assert runs == [1, 1]


def test_housekeeping_recovers_jobs_fails_abandoned_uploads_and_sweeps_staged_files(
    org: Org_, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    queue()
    make_stale(claimed(OTHER_WORKER).id)
    visit_id = quiet_visit(org)
    staged = old_staged_file(tmp_path)

    with caplog.at_level(logging.INFO):
        counts = worker.Worker(WORKER_ID).housekeep()

    assert counts == {
        "stale_queued": 1,
        "stale_dead": 0,
        "abandoned_uploads": 1,
        "staged_files": 1,
        "extractions_queued": 0,  # no MODEL_API_KEY in tests: extraction is off
        "sessions_ended": 0,
        "signin_failures_forgotten": 0,
        "retention_queued": 1,  # the day's first: due in a few minutes (batch 17)
    }
    assert load(visit_id).status == VisitStatus.FAILED
    assert not staged.exists()
    assert "housekeeping stale_queued=1 stale_dead=0 abandoned_uploads=1 staged_files=1" in (
        caplog.text
    )


def test_a_housekeeping_step_that_fails_doesnt_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(session: object) -> int:
        raise OperationalError("SELECT ... FOR UPDATE", None, Exception("lock timeout"))

    monkeypatch.setattr(visits, "fail_abandoned_uploads", broken)
    staged = old_staged_file(tmp_path)

    counts = worker.Worker(WORKER_ID).housekeep()

    assert "abandoned_uploads" not in counts
    assert counts["staged_files"] == 1
    assert not staged.exists()
    assert "housekeeping step failed step=abandoned_uploads" in caplog.text


# --- stopping ----------------------------------------------------------------------------------


def test_a_stop_during_a_job_lets_it_finish_then_stops() -> None:
    first, second = queue(), queue()
    the_worker = worker.Worker(WORKER_ID)
    the_worker.handlers = {JobKind.PING: lambda ctx: the_worker.request_stop(signal.SIGTERM)}

    the_worker.run()

    assert job_row(first).state == JobState.DONE
    assert job_row(second).state == JobState.QUEUED


def test_an_idle_worker_stops_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "WORKER_IDLE_MS", 60_000)
    the_worker = worker.Worker(WORKER_ID)
    # A daemon, so if it doesn't stop, the test fails instead of holding up the run.
    thread = threading.Thread(target=the_worker.run, daemon=True)
    thread.start()
    time.sleep(0.3)

    the_worker.request_stop(signal.SIGTERM)
    thread.join(5)

    assert not thread.is_alive()


def test_a_second_sigint_quits_at_once() -> None:
    the_worker = worker.Worker(WORKER_ID)
    the_worker.request_stop(signal.SIGINT)

    with pytest.raises(KeyboardInterrupt):
        the_worker.request_stop(signal.SIGINT)


def test_a_second_sigterm_still_lets_the_job_finish() -> None:
    the_worker = worker.Worker(WORKER_ID)
    the_worker.request_stop(signal.SIGTERM)

    try:
        the_worker.request_stop(signal.SIGTERM)
    except KeyboardInterrupt:  # caught here: unhandled, it would end the whole test run
        pytest.fail("a second SIGTERM quit at once")

    assert the_worker.stopping.is_set()


# --- the real process (POSIX: signals to a child) -------------------------------------------------


@pytest.fixture
def worker_process(tmp_path: Path) -> Iterator[Callable[[], subprocess.Popen[str]]]:
    started: list[subprocess.Popen[str]] = []
    log_path = tmp_path / "worker.log"

    def start() -> subprocess.Popen[str]:
        env = {**os.environ, **PINNED_SETTINGS, "DATA_DIR": str(tmp_path)}
        with log_path.open("w") as sink:
            process = subprocess.Popen(
                [sys.executable, "-m", "countertalk.worker"],
                cwd=BACKEND_DIR,
                env=env,
                stdout=sink,
                stderr=subprocess.STDOUT,
                text=True,
            )
        started.append(process)
        wait_for(lambda: "worker started" in log_path.read_text(), "the worker didn't start")
        return process

    yield start
    for process in started:
        if process.poll() is None:
            process.kill()
            process.wait()


def wait_for(condition: Callable[[], bool], failure: str) -> None:
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while not condition():
        assert time.monotonic() < deadline, failure
        time.sleep(0.1)


def job_state(job_id: str) -> str:
    with session_scope(read_only=True) as session:
        state = session.scalar(select(Job.state).where(Job.id == job_id))
        assert state is not None
        return state


def test_an_error_escaping_the_worker_prints_only_its_frames(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "from countertalk import worker\n"
        "def broken(self):\n"
        "    raise RuntimeError('Key (email)=(leaky.person@example.com) already exists')\n"
        "worker.Worker.run = broken\n"
        "sys.exit(worker.main())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env={**os.environ, **PINNED_SETTINGS, "DATA_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
    )

    assert result.returncode != 0
    assert "RuntimeError" in result.stderr
    assert "leaky" not in result.stdout + result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can't send SIGTERM to a child")
def test_sigterm_stops_an_idle_worker_cleanly(
    worker_process: Callable[[], subprocess.Popen[str]], tmp_path: Path
) -> None:
    process = worker_process()

    process.send_signal(signal.SIGTERM)

    assert process.wait(timeout=RUN_TIMEOUT_S) == 0
    assert "worker stopped" in (tmp_path / "worker.log").read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows can't send SIGINT to a child")
def test_a_second_ctrl_c_quits_in_the_middle_of_a_job(
    worker_process: Callable[[], subprocess.Popen[str]],
) -> None:
    job_id = queue(JobKind.SLEEP)  # SLEEP_JOB_MS, far longer than this test waits
    process = worker_process()
    wait_for(lambda: job_state(job_id) == JobState.RUNNING, "the sleep job didn't start")

    process.send_signal(signal.SIGINT)
    time.sleep(0.5)
    assert process.poll() is None  # the first one lets the job finish
    process.send_signal(signal.SIGINT)

    assert process.wait(timeout=5) == 130
    assert job_state(job_id) == JobState.RUNNING  # left for stale recovery
