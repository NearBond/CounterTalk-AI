"""speaker roles and llm calls

Revision ID: e4f9fe803b4c
Revises: da6c5c626cf2
Create Date: 2026-11-01 16:05:57.162050
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4f9fe803b4c"
down_revision: str | Sequence[str] | None = "da6c5c626cf2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_hash", sa.String(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("purpose IN ('roles')", name=op.f("ck_llm_calls_purpose")),
        sa.CheckConstraint("ok = (error IS NULL)", name=op.f("ck_llm_calls_ok_or_error")),
        sa.ForeignKeyConstraint(
            ["visit_id"],
            ["visits.id"],
            name=op.f("fk_llm_calls_visit_id_visits"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_calls")),
        sa.UniqueConstraint("seq", name=op.f("uq_llm_calls_seq")),
    )
    op.create_index(op.f("ix_llm_calls_visit_id"), "llm_calls", ["visit_id"], unique=False)
    op.create_table(
        "speaker_roles",
        sa.Column("transcript_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("speaker_label", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("playbook_id", sa.String(), nullable=False),
        sa.Column("playbook_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_speaker_roles_confidence")
        ),
        sa.CheckConstraint("playbook_version >= 1", name=op.f("ck_speaker_roles_playbook_version")),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["transcripts.id"],
            name=op.f("fk_speaker_roles_transcript_id_transcripts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("transcript_id", "speaker_label", name=op.f("pk_speaker_roles")),
    )


def downgrade() -> None:
    op.drop_table("speaker_roles")
    op.drop_index(op.f("ix_llm_calls_visit_id"), table_name="llm_calls")
    op.drop_table("llm_calls")
