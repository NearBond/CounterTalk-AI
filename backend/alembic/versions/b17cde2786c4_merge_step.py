"""merge step

Revision ID: b17cde2786c4
Revises: 68eaed90e048
Create Date: 2026-11-02 06:58:02.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b17cde2786c4"
down_revision: str | Sequence[str] | None = "68eaed90e048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _checks(purposes: str, summary_purposes: str) -> None:
    # Autogenerate doesn't compare check constraints: these are changed by hand.
    op.drop_constraint(op.f("ck_llm_calls_summary"), "llm_calls", type_="check")
    op.drop_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_llm_calls_purpose"), "llm_calls", f"purpose IN ({purposes})"
    )
    op.create_check_constraint(
        op.f("ck_llm_calls_summary"),
        "llm_calls",
        f"(purpose IN ({summary_purposes})) = (summary_id IS NOT NULL)",
    )


def upgrade() -> None:
    op.add_column(
        "summaries",
        sa.Column("merge_step", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    _checks("'roles', 'extraction', 'quote_repair', 'summary', 'merge'", "'summary', 'merge'")


def downgrade() -> None:
    # The merge calls go with their purpose: the old checks would refuse them.
    op.execute("DELETE FROM llm_calls WHERE purpose = 'merge'")
    _checks("'roles', 'extraction', 'quote_repair', 'summary'", "'summary'")
    op.drop_column("summaries", "merge_step")
