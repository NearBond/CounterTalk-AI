"""jobs last error code

Revision ID: e2526c130c39
Revises: b2e9fce36da2
Create Date: 2026-11-01 21:51:38.300628
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2526c130c39"
down_revision: str | Sequence[str] | None = "b2e9fce36da2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("last_error_code", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "last_error_code")
