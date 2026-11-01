"""alias reviews

Revision ID: 3e9b5fc94d3a
Revises: c4e36358fd6d
Create Date: 2026-11-01 20:12:35.855855
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e9b5fc94d3a"
down_revision: str | Sequence[str] | None = "c4e36358fd6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alias_reviews",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("normalised", sa.String(), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('own', 'competitor')", name=op.f("ck_alias_reviews_kind")),
        sa.CheckConstraint(
            "status IN ('open', 'attached', 'created', 'dismissed')",
            name=op.f("ck_alias_reviews_status"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "visit_id"],
            ["visits.org_id", "visits.id"],
            name=op.f("fk_alias_reviews_org_id_visits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_alias_reviews_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alias_reviews")),
        sa.UniqueConstraint(
            "org_id",
            "visit_id",
            "kind",
            "normalised",
            name=op.f("uq_alias_reviews_org_id_visit_id_kind_normalised"),
        ),
    )


def downgrade() -> None:
    op.drop_table("alias_reviews")
