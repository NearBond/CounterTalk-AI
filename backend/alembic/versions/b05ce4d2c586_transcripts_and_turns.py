"""transcripts and turns

Revision ID: b05ce4d2c586
Revises: 13b73dd05fd7
Create Date: 2026-11-01 11:06:36.900776
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b05ce4d2c586"
down_revision: str | Sequence[str] | None = "13b73dd05fd7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Generic, so any append-only table can use it: batch 12 attaches it to routing_decisions too.
# A delete that cascades from a parent row runs inside the foreign key's own trigger
# (pg_trigger_depth() > 1), and is let through: a row goes only with what it belongs to.
REFUSE_CHANGE = """
CREATE FUNCTION refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% rows never change', TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "transcripts",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("language_code", sa.String(), nullable=True),
        sa.Column("raw_dir", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["visit_id"],
            ["visits.id"],
            name=op.f("fk_transcripts_visit_id_visits"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transcripts")),
        sa.UniqueConstraint("visit_id", "id", name=op.f("uq_transcripts_visit_id_id")),
    )
    op.create_table(
        "turns",
        sa.Column("transcript_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("turn_id", sa.Integer(), nullable=False),
        sa.Column("speaker_label", sa.String(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.CheckConstraint("start_ms >= 0 AND end_ms >= start_ms", name=op.f("ck_turns_times")),
        sa.ForeignKeyConstraint(
            ["transcript_id"],
            ["transcripts.id"],
            name=op.f("fk_turns_transcript_id_transcripts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("transcript_id", "turn_id", name=op.f("pk_turns")),
    )
    op.execute(REFUSE_CHANGE)
    op.execute(
        "CREATE TRIGGER turns_never_change BEFORE UPDATE OR DELETE ON turns"
        " FOR EACH ROW EXECUTE FUNCTION refuse_change()"
    )
    op.add_column("visits", sa.Column("transcript_id", sa.Uuid(as_uuid=False), nullable=True))
    op.create_foreign_key(
        op.f("fk_visits_id_transcripts"),
        "visits",
        "transcripts",
        ["id", "transcript_id"],
        ["visit_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_visits_id_transcripts"), "visits", type_="foreignkey")
    op.drop_column("visits", "transcript_id")
    op.drop_table("turns")  # its trigger goes with it
    op.execute("DROP FUNCTION refuse_change()")
    op.drop_table("transcripts")
