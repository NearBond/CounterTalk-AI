"""Size, duration and count limits (CODING_GUIDELINES §1.6). Tests import them, not the numbers."""

# Set on every database connection (BRIEF §7): a stuck lock fails fast, and a transaction left
# open by mistake is killed.
LOCK_TIMEOUT_MS = 5_000
IDLE_IN_TRANSACTION_SESSION_TIMEOUT_MS = 60_000

# How long opening a database connection may take before it counts as unreachable.
DB_CONNECT_TIMEOUT_MS = 5_000
