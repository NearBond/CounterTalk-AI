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
