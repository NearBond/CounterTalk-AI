"""SQLAlchemy models: the only place tables are defined (CODING_GUIDELINES §2.1)."""

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta, timezone
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Dialect,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    MetaData,
    String,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Constraint names are fixed, so migrations can refer to them and errors can be matched on them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def new_id() -> str:
    """IDs are UUID4 strings created by the server (§1.5)."""
    return str(uuid.uuid4())


def parse_id(value: str) -> str | None:
    """The canonical form of an ID from outside (lowercase, hyphenated), or None if it isn't one."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)


# India has no daylight saving, so a fixed offset is exact. Only for a local date or for display
# (§1.4): times are stored in UTC.
IST = timezone(timedelta(hours=5, minutes=30))


def ist_date(moment: datetime) -> date:
    if moment.tzinfo is None:
        raise ValueError("naive datetime: times are timezone-aware (§1.4)")
    return moment.astimezone(IST).date()


class UtcDateTime(TypeDecorator[datetime]):
    """timestamptz that refuses naive datetimes and always reads back in UTC (§1.4)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("naive datetime: pass a timezone-aware one, such as utc_now()")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        # The driver returns the connection's time zone; callers always get UTC.
        return None if value is None else value.astimezone(UTC)


def _one_of(column: str, values: Iterable[str]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


class Role(StrEnum):
    REP = "rep"
    TEAM_MEMBER = "team_member"
    MANAGER = "manager"
    ADMIN = "admin"


class Org(Base):
    __tablename__ = "orgs"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String, unique=True)
    # The org pack it runs on (batch 8): its teams, routing, schemes and the catalog it seeded.
    org_pack_id: Mapped[str | None] = mapped_column(String)
    org_pack_version: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Team(Base):
    """One of an org's teams, seeded by key from its org pack (batch 8). What it handles and its
    routing stay in the pack's files."""

    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("org_id", "key"), UniqueConstraint("org_id", "id"))

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    key: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Distributor(Base):
    """A distributor serving the org's shops, imported from its pack's distributors.csv. Issues
    group by it (batch 15): the most likely shared cause of payout and delivery problems."""

    __tablename__ = "distributors"
    __table_args__ = (UniqueConstraint("org_id", "key"), UniqueConstraint("org_id", "id"))

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    key: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    area: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_one_of("role", Role), name="role"),
        # Stored lowercase, so the unique email can't hold the same address twice.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        # The target of visits' (org_id, rep_id) key: a visit's rep is in the visit's org.
        UniqueConstraint("org_id", "id"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"), index=True)
    email: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Shop(Base):
    __tablename__ = "shops"
    # The natural key: seeding and imports match on it. It also serves lookups by org.
    __table_args__ = (
        UniqueConstraint("org_id", "name", "area"),
        # The target of visits' (org_id, shop_id) key: a visit's shop is in the visit's org.
        UniqueConstraint("org_id", "id"),
        # Its distributor is in its own org: the database refuses anything else.
        ForeignKeyConstraint(
            ["org_id", "distributor_id"], ["distributors.org_id", "distributors.id"]
        ),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    name: Mapped[str] = mapped_column(String)
    area: Mapped[str] = mapped_column(String)
    # None until it's known; a shop with none groups by its area (batch 15).
    distributor_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), index=True)
    # The distributor salesman's route that covers it, as the org names it.
    beat: Mapped[str | None] = mapped_column(String)
    # The shop said no to recording: only notes mode is allowed (§7.4, batches 3 and 17).
    do_not_record: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class CatalogKind(StrEnum):
    OWN = "own"
    COMPETITOR = "competitor"


class AliasSource(StrEnum):
    PACK = "pack"
    # From a reviewed unknown name (batch 12), and from the catalog screen (batch 16b).
    REVIEW = "review"
    ADMIN = "admin"


class CatalogItem(Base):
    """A brand or product, the org's own or a competitor's. Seeded from the org pack's CSVs, then
    edited on the catalog screen (batch 16b): an import never changes a row."""

    __tablename__ = "catalog_items"
    __table_args__ = (
        CheckConstraint(_one_of("kind", CatalogKind), name="kind"),
        UniqueConstraint("org_id", "key"),
        UniqueConstraint("org_id", "id"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    # The CSV's key: the natural key an import matches on.
    key: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    company: Mapped[str] = mapped_column(String)
    brand: Mapped[str] = mapped_column(String)
    # None for a brand as a whole.
    product: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(default=True)
    # For compare-and-set edits on the catalog screen (§2.3).
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class CatalogAlias(Base):
    """Another way a catalog item is said or written: Devanagari, spoken forms. Linking matches
    on these (batch 12)."""

    __tablename__ = "catalog_aliases"
    __table_args__ = (
        CheckConstraint(_one_of("source", AliasSource), name="source"),
        ForeignKeyConstraint(
            ["org_id", "item_id"], ["catalog_items.org_id", "catalog_items.id"], ondelete="CASCADE"
        ),
        UniqueConstraint("item_id", "text"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"), index=True)
    item_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    text: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class VisitStatus(StrEnum):
    """BRIEF §6. Only visits.set_status() changes it, against its table of allowed steps."""

    RECORDING = "recording"
    UPLOADED = "uploaded"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    EXTRACTING = "extracting"
    REVIEW = "review"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class VisitKind(StrEnum):
    CONVERSATION = "conversation"
    # The rep's own summary, for a shop that said no to recording (batch 17).
    NOTES = "notes"


class VisitClient(StrEnum):
    WEB = "web"
    UPLOAD = "upload"
    PHONE = "phone"


class Visit(Base):
    """One recorded conversation at one shop, uploaded as numbered segments of one stream."""

    __tablename__ = "visits"
    __table_args__ = (
        CheckConstraint(_one_of("status", VisitStatus), name="status"),
        CheckConstraint(_one_of("kind", VisitKind), name="kind"),
        CheckConstraint(_one_of("client", VisitClient), name="client"),
        # Newest first within an org; seq breaks created_at ties (BRIEF §7).
        Index("ix_visits_org_id_created_at_seq", "org_id", "created_at", "seq"),
        # The target of insights' (org_id, visit_id): an insight's org is its visit's.
        UniqueConstraint("org_id", "id"),
        # The shop and the rep belong to the visit's own org: the database refuses anything else.
        ForeignKeyConstraint(["org_id", "shop_id"], ["shops.org_id", "shops.id"]),
        ForeignKeyConstraint(["org_id", "rep_id"], ["users.org_id", "users.id"]),
        # The current transcript is one of the visit's own. Added after both tables exist.
        ForeignKeyConstraint(
            ["id", "transcript_id"], ["transcripts.visit_id", "transcripts.id"], use_alter=True
        ),
    )
    # The insert returns seq, so it's there after a flush without another query.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    shop_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    rep_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    kind: Mapped[str] = mapped_column(String)
    client: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=VisitStatus.RECORDING.value)
    # Written for the rep to read; never an exception's text (§1.9).
    failure_reason: Mapped[str | None] = mapped_column(String)
    # The uploaded stream's extension, from complete(); storage.check_ext() allows it first.
    source_ext: Mapped[str | None] = mapped_column(String)
    # The current transcript (batch 6). Reprocessing makes a new one; turns never change.
    transcript_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    duration_ms: Mapped[int | None]
    stt_seconds_billed: Mapped[int] = mapped_column(default=0)
    stt_wall_ms: Mapped[int] = mapped_column(default=0)
    abandoned_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class Segment(Base):
    """One uploaded piece of a visit's stream, opaque bytes until batch 5 joins them (BRIEF §2)."""

    __tablename__ = "segments"
    __table_args__ = (
        CheckConstraint("seq >= 0", name="seq_not_negative"),
        CheckConstraint("bytes > 0", name="bytes_positive"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
    )

    visit_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("visits.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(primary_key=True)
    # Relative to DATA_DIR (storage.relative_to_data_dir), so the folder can move.
    path: Mapped[str] = mapped_column(String)
    bytes: Mapped[int]
    sha256: Mapped[str] = mapped_column(String)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Transcript(Base):
    """One transcription of a visit (batch 6). raw_dir holds each chunk's reply as it came."""

    __tablename__ = "transcripts"
    # The target of visits' (id, transcript_id) key: a visit's transcript is its own.
    __table_args__ = (UniqueConstraint("visit_id", "id"),)

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    visit_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("visits.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    # The languages recognition was biased towards, as ISO 639-1 codes ("hi,en"), or None.
    language_code: Mapped[str | None] = mapped_column(String)
    # Relative to DATA_DIR (storage.relative_to_data_dir), so the folder can move.
    raw_dir: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Turn(Base):
    """One speaker's turn, in visit time. Never changed: a trigger refuses UPDATE and DELETE,
    except a delete that cascades from its visit (the migration's refuse_change())."""

    __tablename__ = "turns"
    __table_args__ = (CheckConstraint("start_ms >= 0 AND end_ms >= start_ms", name="times"),)

    transcript_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("transcripts.id", ondelete="CASCADE"), primary_key=True
    )
    # 0, 1, 2, … in time order across the visit's chunks.
    turn_id: Mapped[int] = mapped_column(primary_key=True)
    # Speech-to-text's label, prefixed with its chunk: c0:A, c1:A.
    speaker_label: Mapped[str] = mapped_column(String)
    start_ms: Mapped[int]
    end_ms: Mapped[int]
    text: Mapped[str] = mapped_column(String)


class SpeakerRole(Base):
    """Who one of a transcript's speaker labels is (batch 9): one of its playbook's roles. Saved
    once, for every label, and never asked for again (§9.4)."""

    __tablename__ = "speaker_roles"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence"),
        CheckConstraint("playbook_version >= 1", name="playbook_version"),
    )

    transcript_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("transcripts.id", ondelete="CASCADE"), primary_key=True
    )
    # As on its turns: c0:A, c1:?.
    speaker_label: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String)
    # The model's own, 0 to 1; 0 for a label it didn't place.
    confidence: Mapped[float]
    playbook_id: Mapped[str] = mapped_column(String)
    playbook_version: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class LlmPurpose(StrEnum):
    ROLES = "roles"
    EXTRACTION = "extraction"
    QUOTE_REPAIR = "quote_repair"


class LlmCall(Base):
    """One request to the LLM (batch 9), failed ones too, for what it cost (§9.3; BRIEF §8).
    Never the prompt or the answer (§1.9): a hash of the request instead."""

    __tablename__ = "llm_calls"
    __table_args__ = (
        CheckConstraint(_one_of("purpose", LlmPurpose), name="purpose"),
        # An answered call has no error, and a failed one has its code.
        CheckConstraint("ok = (error IS NULL)", name="ok_or_error"),
    )
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    # Breaks created_at ties: one answer's requests are written together.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    # None for a call that isn't about one visit (batch 15b's summaries), or whose visit was
    # deleted: what it cost stays counted.
    visit_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("visits.id", ondelete="SET NULL"), index=True
    )
    purpose: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)
    prompt_hash: Mapped[str] = mapped_column(String)
    # None where the reply didn't say. Reasoning tokens are billed as output.
    input_tokens: Mapped[int | None]
    cached_input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    reasoning_tokens: Mapped[int | None]
    latency_ms: Mapped[int]
    request_id: Mapped[str | None] = mapped_column(String)
    ok: Mapped[bool]
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"


# A visit has at most one active job of each kind (BRIEF §7). A retry is queued again.
ACTIVE_JOB_STATES = (JobState.QUEUED, JobState.RUNNING)
# The partial unique index's condition, as SQL: enqueue's ON CONFLICT names it to find the index.
ACTIVE_JOB = _one_of("state", ACTIVE_JOB_STATES)


class JobKind(StrEnum):
    """BRIEF §6. Each kind's max attempts are in jobs.KINDS, its handler in worker.py."""

    TRANSCRIBE = "transcribe"
    EXTRACT = "extract"
    SUMMARY = "summary"
    RETENTION = "retention"
    # Test jobs.
    PING = "ping"
    SLEEP = "sleep"


class Job(Base):
    """Background work, claimed by one worker at a time (jobs.py).

    Its times all come from Postgres's now(), so workers on different machines agree on what is
    due and what is stale.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_one_of("state", JobState), name="state"),
        CheckConstraint(_one_of("kind", JobKind), name="kind"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts"),
        # Running means claimed: the job says which worker has it and since when.
        CheckConstraint(
            f"(state = '{JobState.RUNNING}') = (locked_by IS NOT NULL AND locked_at IS NOT NULL)",
            name="claimed_when_running",
        ),
        Index(
            "uq_jobs_active_visit_id_kind",
            "visit_id",
            "kind",
            unique=True,
            postgresql_where=text(ACTIVE_JOB),
        ),
        # The claim's order, over queued jobs only.
        Index(
            "ix_jobs_queued_created_at_seq",
            "created_at",
            "seq",
            postgresql_where=text(f"state = '{JobState.QUEUED}'"),
        ),
    )
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    # None for work that isn't about one visit: the test jobs, and batch 15b's summaries.
    visit_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("visits.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default=JobState.QUEUED.value)
    # Claims so far, this one included.
    attempts: Mapped[int] = mapped_column(default=0)
    # Fixed when the job is queued, from its kind (jobs.KINDS).
    max_attempts: Mapped[int]
    run_after: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    # Only words we wrote: a foreign exception's text can hold personal data (§1.9).
    last_error: Mapped[str | None] = mapped_column(String)
    # Its code, stored to be matched on (batch 13's reprocess_failed.py; R4): a DomainError's,
    # unexpected_error or stale_claim. None for a job that failed before batch 13.
    last_error_code: Mapped[str | None] = mapped_column(String)
    locked_by: Mapped[str | None] = mapped_column(String)
    locked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())


class Extraction(Base):
    """One extraction run's answer, as it came (batch 10). Every run adds one; the report, tasks
    and insights are replaced from the newest, before the visit is confirmed."""

    __tablename__ = "extractions"
    __table_args__ = (
        # Its transcript is one of its visit's own.
        ForeignKeyConstraint(
            ["visit_id", "transcript_id"], ["transcripts.visit_id", "transcripts.id"]
        ),
        # The target of reports' and insights' (visit_id, extraction_id).
        UniqueConstraint("visit_id", "id"),
    )
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    # Breaks created_at ties: the newest run is the highest.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    visit_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("visits.id", ondelete="CASCADE"), index=True
    )
    transcript_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    playbook_id: Mapped[str] = mapped_column(String)
    playbook_version: Mapped[int]
    org_pack_id: Mapped[str] = mapped_column(String)
    org_pack_version: Mapped[int]
    model: Mapped[str] = mapped_column(String)
    # The request's hash, as in llm_calls: never the prompt (§1.9).
    prompt_hash: Mapped[str] = mapped_column(String)
    raw_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class Report(Base):
    """A visit's report, from its newest extraction."""

    __tablename__ = "reports"
    __table_args__ = (
        # Its extraction is one of its visit's own.
        ForeignKeyConstraint(
            ["visit_id", "extraction_id"],
            ["extractions.visit_id", "extractions.id"],
            ondelete="CASCADE",
        ),
    )

    visit_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("visits.id", ondelete="CASCADE"), primary_key=True
    )
    extraction_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    summary_en: Mapped[str] = mapped_column(String)
    next_visit_date: Mapped[date | None]
    # The answer's report-level fields: what a later schema adds lands here with no migration.
    json: Mapped[dict] = mapped_column(JSONB)


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"


class Task(Base):
    """A follow-up the visit asked for. Its type and owner are the playbook's keys."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(_one_of("status", TaskStatus), name="status"),
        UniqueConstraint("visit_id", "seq"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    visit_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("visits.id", ondelete="CASCADE")
    )
    # 0, 1, 2, … in the answer's order.
    seq: Mapped[int]
    type: Mapped[str] = mapped_column(String)
    description_en: Mapped[str] = mapped_column(String)
    due: Mapped[date | None]
    owner: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default=TaskStatus.OPEN.value)


class Insight(Base):
    """One thing the shop raised (batch 10). Its kind, category and severity are the playbook's
    keys. It has no team or status of its own: its teams are its routes (batch 12), and its
    status is its issues' (batch 15)."""

    __tablename__ = "insights"
    __table_args__ = (
        UniqueConstraint("visit_id", "seq"),
        # The target of evidence's (insight_id, visit_id).
        UniqueConstraint("id", "visit_id"),
        # The target of its routes' (org_id, insight_id): a route's team is the insight's org's.
        UniqueConstraint("org_id", "id"),
        # An unverified insight never reaches a team (BRIEF D9).
        CheckConstraint("verified OR NOT visible", name="visible_only_verified"),
        # Extracted, or added by the rep: one or the other.
        CheckConstraint("added_by_rep = (extraction_id IS NULL)", name="extracted_or_added"),
        # Its extraction is one of its visit's own.
        ForeignKeyConstraint(
            ["visit_id", "extraction_id"],
            ["extractions.visit_id", "extractions.id"],
            ondelete="CASCADE",
            name="fk_insights_extraction_id_extractions",
        ),
        # Its visit, catalog items and suggested team are all its own org's.
        ForeignKeyConstraint(
            ["org_id", "visit_id"], ["visits.org_id", "visits.id"], ondelete="CASCADE"
        ),
        # Named for their own column: each starts with org_id, which the convention would use.
        ForeignKeyConstraint(
            ["org_id", "sku_id"],
            ["catalog_items.org_id", "catalog_items.id"],
            name="fk_insights_sku_id_catalog_items",
        ),
        ForeignKeyConstraint(
            ["org_id", "competitor_id"],
            ["catalog_items.org_id", "catalog_items.id"],
            name="fk_insights_competitor_id_catalog_items",
        ),
        ForeignKeyConstraint(
            ["org_id", "ai_team_id"],
            ["teams.org_id", "teams.id"],
            name="fk_insights_ai_team_id_teams",
        ),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    visit_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    # 0, 1, 2, … within the visit.
    seq: Mapped[int]
    # The run it came from; None for one the rep added (batch 14).
    extraction_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    kind: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    summary_en: Mapped[str] = mapped_column(String)
    # As said; linked to the catalog in batch 12.
    sku_mentions: Mapped[list[str]] = mapped_column(ARRAY(String))
    competitor_mentions: Mapped[list[str]] = mapped_column(ARRAY(String))
    competitor_reason: Mapped[str | None] = mapped_column(String)
    sku_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    competitor_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    # One of the org pack's schemes, as the answer gave it (batch 12 drops an unknown one).
    scheme_key: Mapped[str | None] = mapped_column(String)
    # The AI's suggestion, never the insight's team (BRIEF D7).
    ai_team_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    ai_team_reason: Mapped[str | None] = mapped_column(String)
    added_by_rep: Mapped[bool] = mapped_column(default=False)
    # Its quotes checked against the transcript (batch 11); only then can it be shown.
    verified: Mapped[bool] = mapped_column(default=False)
    visible: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class EvidenceMatch(StrEnum):
    EXACT = "exact"
    FUZZY = "fuzzy"
    NONE = "none"


class Evidence(Base):
    """One quote behind an insight. The model's words are kept as they came; batch 11 checks them
    against the transcript and fills in the rest."""

    __tablename__ = "evidence"
    __table_args__ = (
        UniqueConstraint("insight_id", "seq"),
        # Its insight and its transcript are both its visit's: never another visit's words.
        ForeignKeyConstraint(
            ["insight_id", "visit_id"], ["insights.id", "insights.visit_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["visit_id", "transcript_id"],
            ["transcripts.visit_id", "transcripts.id"],
            ondelete="CASCADE",
        ),
        # Its turn is one of the transcript's: where the quote was found, or for one that
        # wasn't, the cited turn, or no turn if the transcript hasn't got it. Never dropped.
        ForeignKeyConstraint(
            ["transcript_id", "turn_id"],
            ["turns.transcript_id", "turns.turn_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(f"match IS NULL OR {_one_of('match', EvidenceMatch)}", name="match"),
        CheckConstraint(
            "char_start IS NULL OR (char_start >= 0 AND char_end >= char_start)", name="chars"
        ),
        CheckConstraint("start_ms IS NULL OR start_ms >= 0", name="start_ms"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    insight_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    visit_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    seq: Mapped[int]
    transcript_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    turn_id: Mapped[int | None]
    # That turn's speaker, from its saved role.
    speaker_role: Mapped[str | None] = mapped_column(String)
    quote_llm: Mapped[str] = mapped_column(String)
    quote_final: Mapped[str | None] = mapped_column(String)
    match: Mapped[str | None] = mapped_column(String)
    char_start: Mapped[int | None]
    char_end: Mapped[int | None]
    start_ms: Mapped[int | None]


class AliasReviewStatus(StrEnum):
    OPEN = "open"
    # The catalog screen's three ways to resolve one (batch 16b).
    ATTACHED = "attached"
    CREATED = "created"
    DISMISSED = "dismissed"


class AliasReview(Base):
    """A name linking couldn't place (batch 12): in no catalog item, or as near to two as to one.
    Never linked to the nearest guess (§9.6): the catalog screen resolves it (batch 16b)."""

    __tablename__ = "alias_reviews"
    __table_args__ = (
        CheckConstraint(_one_of("kind", CatalogKind), name="kind"),
        CheckConstraint(_one_of("status", AliasReviewStatus), name="status"),
        # Once per name per visit: linking again adds nothing (§2.5). Visit second: it also
        # serves the foreign key below, and a visit's own reviews.
        UniqueConstraint("org_id", "visit_id", "kind", "normalised"),
        # Its visit is its own org's, and it goes with the visit.
        ForeignKeyConstraint(
            ["org_id", "visit_id"], ["visits.org_id", "visits.id"], ondelete="CASCADE"
        ),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    # The kind of the list the name was in: the org's own, or a competitor's.
    kind: Mapped[str] = mapped_column(String)
    # As the model wrote it.
    text: Mapped[str] = mapped_column(String)
    # Its linking key (entities.link_key): the names that match alike are one review.
    normalised: Mapped[str] = mapped_column(String)
    visit_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    status: Mapped[str] = mapped_column(String, default=AliasReviewStatus.OPEN.value)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)


class RoutedBy(StrEnum):
    TABLE = "table"
    AI = "ai"
    OVERRIDE = "override"
    # A team member accepted, moved or also sent it (batch 14): "suggested by AI" goes.
    PERSON = "person"


class InsightRoute(Base):
    """One team an insight is sent to (batch 12; BRIEF D7). Slot 1 is its main team; slot 2 is
    its category's override or a team member's Also send. An insight with no route is in triage.

    Only routing.py writes these (an architecture test), each change with its RoutingDecision
    row. A route goes with its insight (cascade): a re-extraction's new insights are routed
    afresh, and the history stays in routing_decisions."""

    __tablename__ = "insight_routes"
    __table_args__ = (
        CheckConstraint(_one_of("routed_by", RoutedBy), name="routed_by"),
        # The cap on an insight's teams is this one constraint: raising it is a small change.
        CheckConstraint("slot BETWEEN 1 AND 2", name="slot"),
        UniqueConstraint("insight_id", "slot"),
        UniqueConstraint("insight_id", "team_id"),
        ForeignKeyConstraint(
            ["org_id", "insight_id"], ["insights.org_id", "insights.id"], ondelete="CASCADE"
        ),
        # Teams are never deleted (they're the pack's), so nothing happens on delete.
        ForeignKeyConstraint(["org_id", "team_id"], ["teams.org_id", "teams.id"]),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    insight_id: Mapped[str] = mapped_column(Uuid(as_uuid=False))
    slot: Mapped[int]
    team_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    routed_by: Mapped[str] = mapped_column(String)
    # For compare-and-set changes (batch 14, §2.3).
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)


class RoutingAction(StrEnum):
    ROUTE = "route"
    OVERRIDE = "override"
    REROUTE = "reroute"
    ACCEPT = "accept"
    MOVE = "move"
    ALSO_SEND = "also_send"
    TRIAGE_ASSIGN = "triage_assign"


# The rule's name for an insight it left without a team.
TRIAGE_RULE = "triage"


class RoutingDecision(Base):
    """The routing history (batch 12; BRIEF §6): a row for each route given or changed, and one
    for an insight the rule left in triage, written in the same transaction as the change. The
    current state is the insight_routes rows.

    Append-only: refuse_change() refuses UPDATE and DELETE, except a delete that cascades from
    the extraction (with its visit). insight_id is a plain indexed UUID with no foreign key, by
    design, so the history outlives an insight a re-extraction replaced. A row from a person
    about a rep's own insight has no extraction, and stays until its org goes. The AI's reason
    isn't copied: it's in the extraction's raw_json."""

    __tablename__ = "routing_decisions"
    __table_args__ = (
        CheckConstraint(_one_of("action", RoutingAction), name="action"),
        # A triage row has neither slot nor team; every other row has both.
        CheckConstraint(
            "(slot IS NULL) = (team_id IS NULL)"
            f" AND (team_id IS NULL) = COALESCE(rule = '{TRIAGE_RULE}', false)",
            name="triage",
        ),
        # Decided by the rule, or by a person: one or the other.
        CheckConstraint("(rule IS NULL) <> (decided_by IS NULL)", name="rule_or_person"),
        # Its team and its person are its org's. Neither is ever deleted while it has history
        # (an UPDATE to null would be refused anyway), so nothing happens on delete.
        ForeignKeyConstraint(["org_id", "team_id"], ["teams.org_id", "teams.id"]),
        ForeignKeyConstraint(["org_id", "decided_by"], ["users.org_id", "users.id"]),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    insight_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    slot: Mapped[int | None]
    team_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    action: Mapped[str] = mapped_column(String)
    # agreed, table_only, ai_filled_gap, disagreed, override:<category> or triage; None for a
    # person's.
    rule: Mapped[str | None] = mapped_column(String)
    decided_by: Mapped[str | None] = mapped_column(Uuid(as_uuid=False))
    # The model's team key when it isn't one of the org's teams: on the slot 1 or triage row.
    ai_team_key_unknown: Mapped[str | None] = mapped_column(String)
    extraction_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), ForeignKey("extractions.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
