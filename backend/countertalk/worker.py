"""The worker: claims jobs and runs them (BRIEF §6, §7; CODING_GUIDELINES §2.4, §9.1, §9.2, §9.5).

Run as `python -m countertalk.worker`; scripts/start.sh starts it next to the server.
- One job at a time. Its handler runs with no transaction open, and opens its own short ones.
- While it runs, a heartbeat thread refreshes its claim every JOB_HEARTBEAT_MS.
- Housekeeping runs at start and every HOUSEKEEPING_INTERVAL_MS: stale jobs, abandoned uploads
  and old staged files. Each step is safe while another worker runs it too.
- The loop survives any single error: it logs, waits WORKER_IDLE_MS and carries on.
- The first SIGTERM or SIGINT lets the current job finish, then the worker exits 0. A second
  SIGINT quits at once (exit 130); the job's claim then goes stale, and it runs again.
"""

import logging
import os
import secrets
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType

# limits as a module, so a test or a manual run can shorten a limit for everything here.
from countertalk import jobs, limits, storage, visits
from countertalk.db import session_scope
from countertalk.errors import DomainError, PermanentError
from countertalk.logs import configure_logging, install_frames_only_excepthook
from countertalk.models import JobKind, JobState

# By name: run as `python -m countertalk.worker`, __name__ is "__main__".
log = logging.getLogger("countertalk.worker")

Handler = Callable[[jobs.ClaimedJob], None]


def _ping(job: jobs.ClaimedJob) -> None:
    log.info("ping %s", jobs.log_fields(job))


def _sleep(job: jobs.ClaimedJob) -> None:
    # Long work to stop a worker in the middle of (batch 4's manual test).
    time.sleep(limits.SLEEP_JOB_MS / 1000)


# Each kind's handler. Its max attempts are in jobs.KINDS; a kind with no handler dies at once.
HANDLERS: dict[str, Handler] = {JobKind.PING: _ping, JobKind.SLEEP: _sleep}


def new_worker_id() -> str:
    """Host, process and a random part, so a restarted process never passes for the old one."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"


class Worker:
    def __init__(
        self,
        worker_id: str | None = None,
        *,
        handlers: dict[str, Handler] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.id = worker_id or new_worker_id()
        self.handlers = HANDLERS if handlers is None else handlers
        self.stopping = threading.Event()
        self._clock = clock
        self._housekeeping_due = clock()

    def run(self) -> None:
        log.info("worker started worker=%s", self.id)
        while not self.stopping.is_set():
            try:
                ran = self.run_once()
            except Exception:
                log.exception("worker turn failed worker=%s", self.id)
                ran = False
            if not ran:
                # The only sleep in the app, and a stop request ends it at once.
                self.stopping.wait(limits.WORKER_IDLE_MS / 1000)
        log.info("worker stopped worker=%s", self.id)

    def run_once(self) -> bool:
        """Housekeeping if it's due, then one job. True if a job ran, so there may be more."""
        if self._clock() >= self._housekeeping_due:
            self._housekeeping_due = self._clock() + limits.HOUSEKEEPING_INTERVAL_MS / 1000
            self.housekeep()
        try:
            with session_scope() as session:
                job = jobs.claim(session, self.id)
        except Exception:
            log.exception("claim failed worker=%s", self.id)
            return False
        if job is None:
            return False
        self.run_job(job)
        return True

    def run_job(self, job: jobs.ClaimedJob) -> None:
        log.info("job started %s worker=%s", jobs.log_fields(job), self.id)
        heartbeat = _Heartbeat(job, self.id)
        heartbeat.start()
        try:
            error = self._run_handler(job)
        finally:
            heartbeat.stop()
        try:
            with session_scope() as session:
                if error is None:
                    jobs.mark_done(session, job, self.id)
                else:
                    jobs.mark_failed(session, job, self.id, error)
        except Exception:
            # The claim is still held, so once it's stale the job is recovered and runs again.
            log.exception("job not marked %s worker=%s", jobs.log_fields(job), self.id)

    def _run_handler(self, job: jobs.ClaimedJob) -> Exception | None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            return PermanentError("unknown_job_kind", f"There's no handler for {job.kind} jobs")
        try:
            handler(job)
        except DomainError as error:
            return error  # mark_failed logs it, in our own words
        except Exception as error:
            # Not ours: its frames help find the cause, and its message is never logged (§2.8).
            log.warning("job raised %s worker=%s", jobs.log_fields(job), self.id, exc_info=True)
            return error
        return None

    def housekeep(self) -> dict[str, int]:
        """Each step on its own, so one that fails doesn't stop the others. Returns the counts."""
        counts: dict[str, int] = {}
        for name, step in _HOUSEKEEPING:
            try:
                counts |= step()
            except Exception:
                log.exception("housekeeping step failed step=%s worker=%s", name, self.id)
        done = " ".join(f"{name}={count}" for name, count in counts.items())
        log.info("housekeeping %s worker=%s", done, self.id)
        return counts

    def request_stop(self, signum: int) -> None:
        """The first signal lets the current job finish. A second SIGINT (Ctrl+C) quits at once."""
        if self.stopping.is_set() and signum == signal.SIGINT:
            raise KeyboardInterrupt
        log.info("worker stopping signal=%s worker=%s", signal.Signals(signum).name, self.id)
        self.stopping.set()


def _recover_stale_jobs() -> dict[str, int]:
    recovered = jobs.recover_stale()
    dead = sum(1 for r in recovered if r.state == JobState.DEAD)
    return {"stale_queued": len(recovered) - dead, "stale_dead": dead}


def _fail_abandoned_uploads() -> dict[str, int]:
    with session_scope() as session:
        return {"abandoned_uploads": visits.fail_abandoned_uploads(session)}


def _sweep_staged_files() -> dict[str, int]:
    return {"staged_files": storage.sweep_staged(limits.STAGED_FILE_MAX_AGE_MS)}


_HOUSEKEEPING: list[tuple[str, Callable[[], dict[str, int]]]] = [
    ("stale_jobs", _recover_stale_jobs),
    ("abandoned_uploads", _fail_abandoned_uploads),
    ("staged_files", _sweep_staged_files),
]


class _Heartbeat:
    """Refreshes a running job's claim every JOB_HEARTBEAT_MS, on its own connection (§9.2)."""

    def __init__(self, job: jobs.ClaimedJob, worker_id: str) -> None:
        self._job = job
        self._worker_id = worker_id
        self._done = threading.Event()
        # A daemon, so a second Ctrl+C never waits for it.
        self._thread = threading.Thread(target=self._beat, name=f"heartbeat-{job.id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        self._thread.join()

    def _beat(self) -> None:
        while not self._done.wait(limits.JOB_HEARTBEAT_MS / 1000):
            try:
                with session_scope() as session:
                    if not jobs.heartbeat(session, self._job, self._worker_id):
                        return  # the claim was lost (jobs logged it), so its result is dropped
            except Exception:
                # The next beat tries again; the stale limit allows for a few missed ones.
                log.exception(
                    "heartbeat failed %s worker=%s", jobs.log_fields(self._job), self._worker_id
                )


def main() -> int:
    configure_logging()
    # Anything that escapes main() or a thread prints its frames, never its message (§1.9).
    install_frames_only_excepthook()
    worker = Worker()

    def on_signal(signum: int, frame: FrameType | None) -> None:
        worker.request_stop(signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        worker.run()
    except KeyboardInterrupt:
        log.warning("worker quit at once worker=%s", worker.id)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
