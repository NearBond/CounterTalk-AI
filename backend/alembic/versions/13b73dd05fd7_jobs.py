"""jobs

Revision ID: 13b73dd05fd7
Revises: f267b8a847f8
Create Date: 2026-11-01 06:32:42.201617
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "13b73dd05fd7"
down_revision: str | Sequence[str] | None = "f267b8a847f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(state = 'running') = (locked_by IS NOT NULL AND locked_at IS NOT NULL)",
            name=op.f("ck_jobs_claimed_when_running"),
        ),
        sa.CheckConstraint(
            "kind IN ('transcribe', 'extract', 'summary', 'retention', 'ping', 'sleep')",
            name=op.f("ck_jobs_kind"),
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'done', 'dead')", name=op.f("ck_jobs_state")
        ),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name=op.f("ck_jobs_attempts")),
        sa.ForeignKeyConstraint(
            ["visit_id"], ["visits.id"], name=op.f("fk_jobs_visit_id_visits"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("seq", name=op.f("uq_jobs_seq")),
    )
    op.create_index(
        "ix_jobs_queued_created_at_seq",
        "jobs",
        ["created_at", "seq"],
        unique=False,
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index(op.f("ix_jobs_visit_id"), "jobs", ["visit_id"], unique=False)
    # At most one active job per (visit, kind); enqueue's ON CONFLICT DO NOTHING relies on it.
    op.create_index(
        "uq_jobs_active_visit_id_kind",
        "jobs",
        ["visit_id", "kind"],
        unique=True,
        postgresql_where=sa.text("state IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_active_visit_id_kind", table_name="jobs")
    op.drop_index(op.f("ix_jobs_visit_id"), table_name="jobs")
    op.drop_index("ix_jobs_queued_created_at_seq", table_name="jobs")
    op.drop_table("jobs")
