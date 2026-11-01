"""extractions, reports, tasks, insights and evidence

Revision ID: 4dea182093b4
Revises: e4f9fe803b4c
Create Date: 2026-11-01 17:22:00.335174
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4dea182093b4"
down_revision: str | Sequence[str] | None = "e4f9fe803b4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # First: insights' (org_id, visit_id) refers to it.
    op.create_unique_constraint(op.f("uq_visits_org_id_id"), "visits", ["org_id", "id"])
    # A new purpose for llm_calls: autogenerate doesn't compare check constraints.
    op.drop_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", type_="check")
    op.create_check_constraint(
        op.f("ck_llm_calls_purpose"), "llm_calls", "purpose IN ('roles', 'extraction')"
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("description_en", sa.String(), nullable=False),
        sa.Column("due", sa.Date(), nullable=True),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'done')", name=op.f("ck_tasks_status")),
        sa.ForeignKeyConstraint(
            ["visit_id"], ["visits.id"], name=op.f("fk_tasks_visit_id_visits"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("visit_id", "seq", name=op.f("uq_tasks_visit_id_seq")),
    )
    op.create_table(
        "extractions",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("transcript_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("playbook_id", sa.String(), nullable=False),
        sa.Column("playbook_version", sa.Integer(), nullable=False),
        sa.Column("org_pack_id", sa.String(), nullable=False),
        sa.Column("org_pack_version", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_hash", sa.String(), nullable=False),
        sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["visit_id", "transcript_id"],
            ["transcripts.visit_id", "transcripts.id"],
            name=op.f("fk_extractions_visit_id_transcripts"),
        ),
        sa.ForeignKeyConstraint(
            ["visit_id"],
            ["visits.id"],
            name=op.f("fk_extractions_visit_id_visits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_extractions")),
        sa.UniqueConstraint("seq", name=op.f("uq_extractions_seq")),
        sa.UniqueConstraint("visit_id", "id", name=op.f("uq_extractions_visit_id_id")),
    )
    op.create_index(op.f("ix_extractions_visit_id"), "extractions", ["visit_id"], unique=False)
    op.create_table(
        "insights",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("summary_en", sa.String(), nullable=False),
        sa.Column("sku_mentions", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("competitor_mentions", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("competitor_reason", sa.String(), nullable=True),
        sa.Column("sku_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("competitor_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("scheme_key", sa.String(), nullable=True),
        sa.Column("ai_team_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("ai_team_reason", sa.String(), nullable=True),
        sa.Column("added_by_rep", sa.Boolean(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("visible", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "added_by_rep = (extraction_id IS NULL)", name=op.f("ck_insights_extracted_or_added")
        ),
        sa.CheckConstraint(
            "verified OR NOT visible", name=op.f("ck_insights_visible_only_verified")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "ai_team_id"],
            ["teams.org_id", "teams.id"],
            name="fk_insights_ai_team_id_teams",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "competitor_id"],
            ["catalog_items.org_id", "catalog_items.id"],
            name="fk_insights_competitor_id_catalog_items",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "sku_id"],
            ["catalog_items.org_id", "catalog_items.id"],
            name="fk_insights_sku_id_catalog_items",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "visit_id"],
            ["visits.org_id", "visits.id"],
            name=op.f("fk_insights_org_id_visits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_insights_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["visit_id", "extraction_id"],
            ["extractions.visit_id", "extractions.id"],
            name="fk_insights_extraction_id_extractions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_insights")),
        sa.UniqueConstraint("id", "visit_id", name=op.f("uq_insights_id_visit_id")),
        sa.UniqueConstraint("visit_id", "seq", name=op.f("uq_insights_visit_id_seq")),
    )
    op.create_table(
        "reports",
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("extraction_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("summary_en", sa.String(), nullable=False),
        sa.Column("next_visit_date", sa.Date(), nullable=True),
        sa.Column("json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["visit_id", "extraction_id"],
            ["extractions.visit_id", "extractions.id"],
            name=op.f("fk_reports_visit_id_extractions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["visit_id"], ["visits.id"], name=op.f("fk_reports_visit_id_visits"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("visit_id", name=op.f("pk_reports")),
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("insight_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("transcript_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=True),
        sa.Column("speaker_role", sa.String(), nullable=True),
        sa.Column("quote_llm", sa.String(), nullable=False),
        sa.Column("quote_final", sa.String(), nullable=True),
        sa.Column("match", sa.String(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "match IS NULL OR match IN ('exact', 'fuzzy', 'none')", name=op.f("ck_evidence_match")
        ),
        sa.CheckConstraint(
            "char_start IS NULL OR (char_start >= 0 AND char_end >= char_start)",
            name=op.f("ck_evidence_chars"),
        ),
        sa.CheckConstraint("start_ms IS NULL OR start_ms >= 0", name=op.f("ck_evidence_start_ms")),
        sa.ForeignKeyConstraint(
            ["insight_id", "visit_id"],
            ["insights.id", "insights.visit_id"],
            name=op.f("fk_evidence_insight_id_insights"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transcript_id", "turn_id"],
            ["turns.transcript_id", "turns.turn_id"],
            name=op.f("fk_evidence_transcript_id_turns"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["visit_id", "transcript_id"],
            ["transcripts.visit_id", "transcripts.id"],
            name=op.f("fk_evidence_visit_id_transcripts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
        sa.UniqueConstraint("insight_id", "seq", name=op.f("uq_evidence_insight_id_seq")),
    )
    op.create_index(op.f("ix_evidence_insight_id"), "evidence", ["insight_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_evidence_insight_id"), table_name="evidence")
    op.drop_table("evidence")
    op.drop_table("reports")
    op.drop_table("insights")
    op.drop_index(op.f("ix_extractions_visit_id"), table_name="extractions")
    op.drop_table("extractions")
    op.drop_table("tasks")
    # Extraction's calls go with their purpose: the old check would refuse them.
    op.execute("DELETE FROM llm_calls WHERE purpose = 'extraction'")
    op.drop_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", type_="check")
    op.create_check_constraint(op.f("ck_llm_calls_purpose"), "llm_calls", "purpose IN ('roles')")
    op.drop_constraint(op.f("uq_visits_org_id_id"), "visits", type_="unique")
