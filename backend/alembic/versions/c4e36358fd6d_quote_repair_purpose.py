"""quote repair purpose

Revision ID: c4e36358fd6d
Revises: 4dea182093b4
Create Date: 2026-11-01 18:33:26.566214
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e36358fd6d"
down_revision: str | Sequence[str] | None = "4dea182093b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _purposes(allowed: str) -> None:
    # Autogenerate doesn't compare check constraints: this one is changed by hand.
    op.drop_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", type_="check")
    op.create_check_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", f"purpose IN ({allowed})")


def upgrade() -> None:
    _purposes("'roles', 'extraction', 'quote_repair'")


def downgrade() -> None:
    # The repairs' calls go with their purpose: the old check would refuse them.
    op.execute("DELETE FROM llm_calls WHERE purpose = 'quote_repair'")
    _purposes("'roles', 'extraction'")
