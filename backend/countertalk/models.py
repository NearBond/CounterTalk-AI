"""SQLAlchemy models: the only place tables are defined (CODING_GUIDELINES §2.1)."""

import uuid
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


def _one_of(column: str, values: type[StrEnum]) -> str:
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
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    name: Mapped[str] = mapped_column(String)
    area: Mapped[str] = mapped_column(String)
    # The shop said no to recording: only notes mode is allowed (§7.4, batches 3 and 17).
    do_not_record: Mapped[bool] = mapped_column(default=False)
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
