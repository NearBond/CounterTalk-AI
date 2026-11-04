"""pre-visit briefs: the briefs, their purpose, and their calls' link

Revision ID: 8378433cf9ef
Revises: c5f2a8e64d17
Create Date: 2026-11-04 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8378433cf9ef"
down_revision: str | Sequence[str] | None = "c5f2a8e64d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = "'roles', 'extraction', 'quote_repair', 'summary', 'merge', 'ask'"


def _purposes(allowed: str) -> None:
    # Autogenerate doesn't compare check constraints: this one is changed by hand.
    op.drop_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", type_="check")
    op.create_check_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", f"purpose IN ({allowed})")


def upgrade() -> None:
    _purposes(f"{BEFORE}, 'brief'")
    op.create_table(
        "briefs",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("shop_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("visits", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("visits >= 2", name=op.f("ck_briefs_visits")),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_briefs_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["org_id", "shop_id"], ["shops.org_id", "shops.id"], name=op.f("fk_briefs_org_id_shops")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"], ["users.org_id", "users.id"], name=op.f("fk_briefs_org_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "visit_id"],
            ["visits.org_id", "visits.id"],
            name=op.f("fk_briefs_org_id_visits"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_briefs")),
    )
    op.create_index(
        "ix_briefs_shop_id_created_at", "briefs", ["shop_id", "created_at"], unique=False
    )
    op.add_column("llm_calls", sa.Column("brief_id", sa.Uuid(as_uuid=False), nullable=True))
    op.create_index(op.f("ix_llm_calls_brief_id"), "llm_calls", ["brief_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_llm_calls_brief_id_briefs"), "llm_calls", "briefs", ["brief_id"], ["id"]
    )
    op.create_check_constraint(
        op.f("ck_llm_calls_brief"), "llm_calls", "brief_id IS NULL OR purpose = 'brief'"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_llm_calls_brief"), "llm_calls", type_="check")
    op.drop_constraint(op.f("fk_llm_calls_brief_id_briefs"), "llm_calls", type_="foreignkey")
    op.drop_index(op.f("ix_llm_calls_brief_id"), table_name="llm_calls")
    op.drop_column("llm_calls", "brief_id")
    op.drop_index("ix_briefs_shop_id_created_at", table_name="briefs")
    op.drop_table("briefs")
    # The briefs' calls go with their purpose: the old check would refuse them.
    op.execute("DELETE FROM llm_calls WHERE purpose = 'brief'")
    _purposes(BEFORE)
