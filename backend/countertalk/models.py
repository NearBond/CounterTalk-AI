"""SQLAlchemy models: the only place tables are defined (CODING_GUIDELINES §2.1)."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Dialect,
    ForeignKey,
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
        CheckConstraint("role IN (" + ", ".join(f"'{role}'" for role in Role) + ")", name="role"),
        # Stored lowercase, so the unique email can't hold the same address twice.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
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
    __table_args__ = (UniqueConstraint("org_id", "name", "area"),)

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("orgs.id"))
    name: Mapped[str] = mapped_column(String)
    area: Mapped[str] = mapped_column(String)
    # The shop said no to recording: only notes mode is allowed (§7.4, batches 3 and 17).
    do_not_record: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
