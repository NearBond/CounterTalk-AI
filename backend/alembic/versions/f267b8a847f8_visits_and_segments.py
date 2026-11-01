"""visits and segments

Revision ID: f267b8a847f8
Revises: 145ac70a1396
Create Date: 2026-11-01 05:36:24.465354
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f267b8a847f8"
down_revision: str | Sequence[str] | None = "145ac70a1396"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # First: visits' (org_id, shop_id) and (org_id, rep_id) keys point at these.
    op.create_unique_constraint(op.f("uq_shops_org_id_id"), "shops", ["org_id", "id"])
    op.create_unique_constraint(op.f("uq_users_org_id_id"), "users", ["org_id", "id"])
    op.create_table(
        "visits",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("shop_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("rep_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("source_ext", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("stt_seconds_billed", sa.Integer(), nullable=False),
        sa.Column("stt_wall_ms", sa.Integer(), nullable=False),
        sa.Column("abandoned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("client IN ('web', 'upload', 'phone')", name=op.f("ck_visits_client")),
        sa.CheckConstraint("kind IN ('conversation', 'notes')", name=op.f("ck_visits_kind")),
        sa.CheckConstraint(
            "status IN ('recording', 'uploaded', 'transcribing', 'transcribed', 'extracting',"
            " 'review', 'confirmed', 'failed')",
            name=op.f("ck_visits_status"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "rep_id"], ["users.org_id", "users.id"], name=op.f("fk_visits_org_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "shop_id"], ["shops.org_id", "shops.id"], name=op.f("fk_visits_org_id_shops")
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_visits_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_visits")),
        sa.UniqueConstraint("seq", name=op.f("uq_visits_seq")),
    )
    op.create_index(
        "ix_visits_org_id_created_at_seq", "visits", ["org_id", "created_at", "seq"], unique=False
    )
    op.create_index(op.f("ix_visits_rep_id"), "visits", ["rep_id"], unique=False)
    op.create_index(op.f("ix_visits_shop_id"), "visits", ["shop_id"], unique=False)
    op.create_table(
        "segments",
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_segments_sha256_hex")),
        sa.CheckConstraint("bytes > 0", name=op.f("ck_segments_bytes_positive")),
        sa.CheckConstraint("seq >= 0", name=op.f("ck_segments_seq_not_negative")),
        sa.ForeignKeyConstraint(
            ["visit_id"],
            ["visits.id"],
            name=op.f("fk_segments_visit_id_visits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("visit_id", "seq", name=op.f("pk_segments")),
    )


def downgrade() -> None:
    op.drop_table("segments")
    op.drop_index(op.f("ix_visits_shop_id"), table_name="visits")
    op.drop_index(op.f("ix_visits_rep_id"), table_name="visits")
    op.drop_index("ix_visits_org_id_created_at_seq", table_name="visits")
    op.drop_table("visits")
    # Last: visits' keys depended on them.
    op.drop_constraint(op.f("uq_users_org_id_id"), "users", type_="unique")
    op.drop_constraint(op.f("uq_shops_org_id_id"), "shops", type_="unique")
