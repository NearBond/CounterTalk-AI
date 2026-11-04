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
# Retention (batch 17): queued by housekeeping once a day; old jobs go with it. A dead job's
# visit keeps its failure_reason.
RETENTION_EVERY_MS = 24 * 3_600_000
# Due this long after it's queued: a worker that starts does the visits' waiting work first.
RETENTION_DELAY_MS = 5 * 60_000
DONE_JOB_KEEP_DAYS = 7
DEAD_JOB_KEEP_DAYS = 30
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

# Transcription (batch 6). Muse Voice Transcribe answers each chunk in one synchronous request;
# each chunk's real time is recorded, so this can be tuned.
STT_TIMEOUT_MS = 300_000
# Brand and product names sent as Muse Voice Transcribe's keywords (batch 8). Meta documents no
# limit (checked 1 Nov), so a cap of our own: brands first, then products.
STT_KEYWORDS_MAX = 50
# Meta refuses a request with a longer keyword (HTTP 400: "ASR keyword 39 exceeds the maximum
# length of 30 characters", 1 Nov). Undocumented, so found by a probe.
STT_KEYWORD_MAX_CHARS = 30
# A provider's own error text on a 4xx (its message, or its type), as kept for the log.
PROVIDER_ERROR_TEXT_MAX = 200

# Muse Spark (batch 9): one request's timeout. Roles answer in seconds; extraction writes a few
# thousand tokens. Long, as for speech-to-text: a request that times out may still be billed, and
# the SDK sends it again (LLM_MAX_RETRIES), so a timeout should mean Meta is stuck, not slow. The
# worker's heartbeat keeps a job claimed however long a call takes.
LLM_TIMEOUT_MS = 300_000
# The SDK's own retries of a request: 408, 409, 429, 5xx and network errors, a timeout included.
# An answer takes up to three requests (the first, one asked again, one repair), each tried up to
# 1 + LLM_MAX_RETRIES times.
LLM_MAX_RETRIES = 2

# Muse Spark's context window, input and output together: "1,048,576-token context window"
# (https://dev.meta.ai/docs/models, 1 Nov). A request that doesn't fit is a 400.
LLM_CONTEXT_TOKENS = 1_048_576
# Room kept for extraction's answer and its reasoning when checking a transcript fits.
LLM_OUTPUT_RESERVE_TOKENS = 32_768

# Extraction (batch 10): the schemes the request lists are those running within this many days
# of the visit's date.
SCHEME_WINDOW_DAYS = 30
# Housekeeping queues extraction for at most this many waiting visits per pass, so a key set
# after a long outage doesn't start every paid call at once.
EXTRACT_QUEUE_PER_PASS = 20

# Quote verification (batch 11): a fuzzy match needs this rapidfuzz partial_ratio score or more.
QUOTE_FUZZY_MIN_SCORE = 92
# ...and a normalised quote this long: a few characters score high almost anywhere.
QUOTE_FUZZY_MIN_CHARS = 10
# Playback starts this long before the quote, into the turn before if need be (D1, 1 Nov).
QUOTE_LEAD_IN_MS = 1_500

# A rep's note on a visit (batch 14): free text for the record, never sent to teams.
NOTE_MAX_CHARS = 2_000
# A rep's summary of an insight, edited or added (batch 14): a sentence or two.
SUMMARY_MAX_CHARS = 500

# Issues (batch 15): a matching insight reopens an issue resolved less than this long ago;
# after it, a new issue starts.
REOPEN_WINDOW_DAYS = 30
# What a team member writes when resolving an issue: the rep reads it back to the shop.
RESOLUTION_NOTE_MAX_CHARS = 2_000
# Fixed since the last visit (batch 17b): the newest this many a shop, on Updates (17e).
FIXED_MAX = 10
# The inbox's period filter (batch 17c): its days are in these years, far wider than any visit
# and well inside what a date can add a day to.
PERIOD_FIRST_YEAR = 2000
PERIOD_LAST_YEAR = 2099
# Ask a question (batch 17d): its length, a user's questions an IST day (the public deploy's
# cost guard), the findings an answer shows, and its last_30_days period.
ASK_QUESTION_MAX_CHARS = 300
ASK_DAILY_MAX = 30
ASK_FINDINGS_MAX = 25
ASK_LAST_DAYS = 30
# A question is asked while the manager waits, not in a job: a short wait, no SDK retries.
ASK_LLM_TIMEOUT_MS = 20_000
ASK_LLM_MAX_RETRIES = 0
# The pre-visit brief (batch F5): a shop needs this many confirmed visits, by any rep. The AI
# writes up to BRIEF_OVERVIEW_SENTENCES about the shop and BRIEF_RAISING_SENTENCES worth
# raising; the facts list up to BRIEF_ITEMS_MAX of each kind. Written while the rep waits.
BRIEF_VISITS_MIN = 2
BRIEF_OVERVIEW_SENTENCES = 4
BRIEF_RAISING_SENTENCES = 2
BRIEF_ITEMS_MAX = 8
BRIEF_LLM_TIMEOUT_MS = 45_000
BRIEF_LLM_MAX_RETRIES = 0

# Summaries' trend labels (batch 15b; BRIEF D16). An issue is `growing` with this many shops in
# the period or more, and a Poisson chance under GROWING_MAX_P of that many at its baseline: the
# mean of shops per period over the previous BASELINE_PERIODS, floored at BASELINE_FLOOR. It's
# `recurring` if seen in RECURRING_MIN_PERIODS of those periods.
GROWING_MIN_SHOPS = 3
GROWING_MAX_P = 0.05
BASELINE_PERIODS = 4
BASELINE_FLOOR = 0.5
RECURRING_MIN_PERIODS = 3
# The narrative (batch 15b): about the top NARRATIVE_ISSUES of the issues with a quote from the
# period, each with up to NARRATIVE_QUOTES quotes (one per shop); an overview of up to
# OVERVIEW_SENTENCES, and up to ISSUE_SENTENCES each.
NARRATIVE_ISSUES = 10
NARRATIVE_QUOTES = 3
OVERVIEW_SENTENCES = 3
ISSUE_SENTENCES = 2
# Merge suggestions (batch 15c): each issue's newest insights' summaries the AI sees.
MERGE_INSIGHT_SUMMARIES = 3
# ...and the longest reason shown with Merge and Dismiss: one plain sentence.
MERGE_REASON_MAX_CHARS = 300

# Linking (batch 12): a fuzzy match needs this rapidfuzz ratio or more on the linking key. From
# the tests and K1 (1 Nov): misspellings of the right item scored 87 and up, and "Crunchy", in
# no pepsico pack, scored 80 against Balaji's "crunchem". No two pepsico names of different
# brands score above 77.
LINK_FUZZY_MIN_SCORE = 85
# ...between keys this long (code points): below it, a letter more or less still scores 85.
LINK_FUZZY_MIN_CHARS = 5
# ...and another item within this many points of the best makes the name ambiguous (§9.6).
LINK_NEAR_POINTS = 5

# Speaker roles (BRIEF §12): the turns sent are the first ROLES_FIRST_TURNS, ROLES_SAMPLED_TURNS
# spread over the rest, and one more for any label not in those.
ROLES_FIRST_TURNS = 60
ROLES_SAMPLED_TURNS = 20

# Sign-in (batch 16): a session lasts this long from sign-in, never extended.
SESSION_DAYS = 14
# ...and wrong passwords for one email: this many are free; past them, a password is checked
# only a second more per failure after the last check, up to SIGNIN_MAX_WAIT_S. A count
# untouched for SIGNIN_FAILURES_KEPT_H starts again.
SIGNIN_FREE_FAILURES = 4
SIGNIN_MAX_WAIT_S = 10
SIGNIN_FAILURES_KEPT_H = 24
# ...and password checks at once: argon2 uses 64 MiB for each, so a flood of sign-ins can't
# take the memory of a small container.
ARGON2_AT_ONCE = 4
# ...and the shortest password set_password.py (and 16's demo org) will take.
PASSWORD_MIN_CHARS = 12
