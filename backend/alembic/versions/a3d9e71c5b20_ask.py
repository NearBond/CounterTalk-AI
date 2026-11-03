"""ask: its purpose, and the questions asked, for the daily limit

Revision ID: a3d9e71c5b20
Revises: 7c1f4e2a9d03
Create Date: 2026-11-03 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3d9e71c5b20"
down_revision: str | Sequence[str] | None = "7c1f4e2a9d03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = "'roles', 'extraction', 'quote_repair', 'summary', 'merge'"


def _purposes(allowed: str) -> None:
    # Autogenerate doesn't compare check constraints: this one is changed by hand.
    op.drop_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", type_="check")
    op.create_check_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", f"purpose IN ({allowed})")


def upgrade() -> None:
    _purposes(f"{BEFORE}, 'ask'")
    op.create_table(
        "asks",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_asks_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_asks_org_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asks")),
    )
    op.create_index("ix_asks_user_id_created_at", "asks", ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_asks_user_id_created_at", table_name="asks")
    op.drop_table("asks")
    # The questions' calls go with their purpose: the old check would refuse them.
    op.execute("DELETE FROM llm_calls WHERE purpose = 'ask'")
    _purposes(BEFORE)
