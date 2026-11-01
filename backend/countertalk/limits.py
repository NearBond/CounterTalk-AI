"""Size, duration and count limits (CODING_GUIDELINES §1.6). Tests import them, not the numbers."""

# Set on every database connection (BRIEF §7): a stuck lock fails fast, and a transaction left
# open by mistake is killed.
LOCK_TIMEOUT_MS = 5_000
IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS = 60_000

# How long opening a database connection may take before it counts as unreachable.
DB_CONNECT_TIMEOUT_MS = 5_000

# scripts/check_setup.py: how long a tool may take to print its version.
SETUP_CHECK_TIMEOUT_MS = 10_000

# One uploaded piece of a visit's audio. Browser segments are 30 s of Opus (well under 1 MiB),
# phone segments 3 min of AAC (a few MiB).
MAX_SEGMENT_BYTES = 8 * 1024 * 1024
# seq runs 0..MAX_SEGMENTS_PER_VISIT - 1: about 16 hours of 30-second browser segments.
MAX_SEGMENTS_PER_VISIT = 2_000

# Visits per page of GET /api/visits.
VISITS_PAGE_SIZE = 50

# The job queue (batch 4). A running job's worker refreshes its claim every JOB_HEARTBEAT_MS; a
# claim not refreshed for STALE_JOB_MS is recovered, so a dead worker's job runs again.
JOB_HEARTBEAT_MS = 60_000
STALE_JOB_MS = 5 * 60_000
# Tries per job, unless its kind sets its own (jobs.KINDS).
JOB_MAX_ATTEMPTS = 5
# A retry waits JOB_BACKOFF_BASE_MS * 2^(attempt - 1), at most JOB_BACKOFF_MAX_MS, give or take
# JOB_BACKOFF_JITTER of it, so jobs that failed together don't all come back together.
JOB_BACKOFF_BASE_MS = 30_000
JOB_BACKOFF_MAX_MS = 10 * 60_000
JOB_BACKOFF_JITTER = 0.2
