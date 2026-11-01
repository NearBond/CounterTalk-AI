"""summaries

Revision ID: d8f1c2bfc9b0
Revises: 8a9d70fcddab
Create Date: 2026-11-02 04:01:46.786476
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8f1c2bfc9b0"
down_revision: str | Sequence[str] | None = "8a9d70fcddab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE_JOB = "state IN ('queued', 'running')"


def _purposes(allowed: str) -> None:
    # Autogenerate doesn't compare check constraints: this one is changed by hand.
    op.drop_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", type_="check")
    op.create_check_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", f"purpose IN ({allowed})")


def upgrade() -> None:
    op.create_table(
        "summaries",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("team_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("numbers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("counted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("narrative", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(scope = 'team') = (team_id IS NOT NULL)", name=op.f("ck_summaries_team")
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_reason IS NOT NULL)", name=op.f("ck_summaries_failed")
        ),
        sa.CheckConstraint("scope IN ('team', 'org')", name=op.f("ck_summaries_scope")),
        sa.CheckConstraint(
            "status <> 'ready' OR (numbers IS NOT NULL AND counted_at IS NOT NULL)",
            name=op.f("ck_summaries_ready"),
        ),
        sa.CheckConstraint(
            "status IN ('writing', 'ready', 'failed')", name=op.f("ck_summaries_status")
        ),
        sa.CheckConstraint("period_end > period_start", name=op.f("ck_summaries_period")),
        sa.ForeignKeyConstraint(
            ["org_id", "team_id"],
            ["teams.org_id", "teams.id"],
            name=op.f("fk_summaries_org_id_teams"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_summaries_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_summaries")),
        sa.UniqueConstraint(
            "org_id",
            "scope",
            "team_id",
            "period_start",
            "period_end",
            name=op.f("uq_summaries_org_id_scope_team_id_period_start_period_end"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.add_column("jobs", sa.Column("summary_id", sa.Uuid(as_uuid=False), nullable=True))
    op.create_index(op.f("ix_jobs_summary_id"), "jobs", ["summary_id"], unique=False)
    op.create_index(
        "uq_jobs_active_summary_id_kind",
        "jobs",
        ["summary_id", "kind"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_JOB),
    )
    op.create_foreign_key(
        op.f("fk_jobs_summary_id_summaries"),
        "jobs",
        "summaries",
        ["summary_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # No summary job was ever queued before this (none had a handler).
    op.create_check_constraint(
        op.f("ck_jobs_summary"), "jobs", "(kind = 'summary') = (summary_id IS NOT NULL)"
    )
    op.create_check_constraint(
        op.f("ck_jobs_visit_or_summary"), "jobs", "summary_id IS NULL OR visit_id IS NULL"
    )
    op.add_column("llm_calls", sa.Column("summary_id", sa.Uuid(as_uuid=False), nullable=True))
    op.create_index(op.f("ix_llm_calls_summary_id"), "llm_calls", ["summary_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_llm_calls_summary_id_summaries"), "llm_calls", "summaries", ["summary_id"], ["id"]
    )
    _purposes("'roles', 'extraction', 'quote_repair', 'summary'")
    op.create_check_constraint(
        op.f("ck_llm_calls_summary"),
        "llm_calls",
        "(purpose = 'summary') = (summary_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_llm_calls_visit_or_summary"),
        "llm_calls",
        "summary_id IS NULL OR visit_id IS NULL",
    )


def downgrade() -> None:
    # The summaries' calls and jobs go with them: the old checks would refuse them.
    op.execute("DELETE FROM llm_calls WHERE purpose = 'summary'")
    op.execute("DELETE FROM jobs WHERE kind = 'summary'")
    op.drop_constraint(op.f("ck_llm_calls_visit_or_summary"), "llm_calls", type_="check")
    op.drop_constraint(op.f("ck_llm_calls_summary"), "llm_calls", type_="check")
    _purposes("'roles', 'extraction', 'quote_repair'")
    op.drop_constraint(op.f("fk_llm_calls_summary_id_summaries"), "llm_calls", type_="foreignkey")
    op.drop_index(op.f("ix_llm_calls_summary_id"), table_name="llm_calls")
    op.drop_column("llm_calls", "summary_id")
    op.drop_constraint(op.f("ck_jobs_visit_or_summary"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_summary"), "jobs", type_="check")
    op.drop_constraint(op.f("fk_jobs_summary_id_summaries"), "jobs", type_="foreignkey")
    op.drop_index(
        "uq_jobs_active_summary_id_kind", table_name="jobs", postgresql_where=sa.text(ACTIVE_JOB)
    )
    op.drop_index(op.f("ix_jobs_summary_id"), table_name="jobs")
    op.drop_column("jobs", "summary_id")
    op.drop_table("summaries")
