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

# The worker (batch 4). Housekeeping runs at start and then every HOUSEKEEPING_INTERVAL_MS.
HOUSEKEEPING_INTERVAL_MS = 10 * 60_000
# A web or upload visit still recording with nothing new for this long is failed as abandoned;
# its tab's next segment, or complete, brings it back (§9.7).
ABANDONED_UPLOAD_MS = 12 * 60 * 60_000
# A staging file older than this was left by a crash; no upload takes anywhere near this long.
STAGED_FILE_MAX_AGE_MS = 60 * 60_000
# How long an idle worker waits before looking for a job again: the only sleep in the app.
WORKER_IDLE_MS = 1_000
# How long the `sleep` test job runs: long enough to kill a worker part-way.
SLEEP_JOB_MS = 30_000

# Audio preparation (batch 5).
# A counter conversation is minutes long; an hour bounds disk, decode time and transcription cost.
MAX_VISIT_MS = 60 * 60_000
# An hour of phone AAC is about 60 MB and of browser Opus about 15 MB: this stops a runaway upload.
MAX_SOURCE_BYTES = 512 * 1024 * 1024
# Muse Voice Transcribe takes 16-bit mono WAV at 16 or 24 kHz; 24 kHz is its native rate.
PREPARED_SAMPLE_RATE = 24_000
# Its limits per file (BRIEF §8): longer is HTTP 400, larger HTTP 413.
MAX_STT_FILE_MS = 10 * 60_000
MAX_STT_FILE_BYTES = 32_000_000
# 9.5 minutes, about 27 MB at 24 kHz: inside both limits, with room.
STT_CHUNK_MS = 570_000
# A pause to cut at lasts at least SILENCE_MIN_MS, as between sentences or turns. silencedetect
# compares each sample's peak, and steady noise peaks well above its average, so pauses are looked
# for at each of these levels and a cut uses the strictest that finds one.
SILENCE_THRESHOLDS_DB = (-35, -30, -25, -20)
SILENCE_MIN_MS = 400
# A cut takes the longest pause in this stretch before the limit: a turn change is usually longer
# than a sentence break. Meta bills audio seconds, so a chunk ending early costs nothing.
PAUSE_SEARCH_MS = 60_000
# Loudest under this is room noise at most, so the recording is refused as "no speech found".
NO_SPEECH_MAX_DB = -45
# A probe reads headers, or at worst every packet of an hour's recording: seconds, not minutes.
FFPROBE_TIMEOUT_MS = 2 * 60_000
# One decode of an hour into WAV and AAC runs many times faster than real time.
FFMPEG_DECODE_TIMEOUT_MS = 15 * 60_000
