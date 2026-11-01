"""SQLAlchemy models: the only place tables are defined (CODING_GUIDELINES §2.1)."""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
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
    locked_by: Mapped[str | None] = mapped_column(String)
    locked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
