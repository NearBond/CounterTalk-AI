"""edits and visit notes

Revision ID: e056cd0ce614
Revises: e2526c130c39
Create Date: 2026-11-01 23:05:19.961220
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e056cd0ce614"
down_revision: str | Sequence[str] | None = "e2526c130c39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "edits",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("object", sa.String(), nullable=False),
        sa.Column("object_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column(
            "before", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "after", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("object IN ('insight', 'visit')", name=op.f("ck_edits_object")),
        sa.CheckConstraint("(before IS NULL) = (field = 'created')", name=op.f("ck_edits_before")),
        sa.CheckConstraint("(after IS NULL) = (field = 'deleted')", name=op.f("ck_edits_after")),
        sa.CheckConstraint(
            "object <> 'visit' OR (object_id = visit_id AND field = 'status')",
            name=op.f("ck_edits_visit_status"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "actor_id"], ["users.org_id", "users.id"], name=op.f("fk_edits_org_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "visit_id"],
            ["visits.org_id", "visits.id"],
            name=op.f("fk_edits_org_id_visits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_edits_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edits")),
    )
    op.create_index(op.f("ix_edits_visit_id"), "edits", ["visit_id"], unique=False)
    # Append-only, through batch 6's refuse_change(): a cascade with its visit still goes.
    op.execute(
        "CREATE TRIGGER edits_never_change BEFORE UPDATE OR DELETE"
        " ON edits FOR EACH ROW EXECUTE FUNCTION refuse_change()"
    )
    op.create_table(
        "visit_notes",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("author_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 2000", name=op.f("ck_visit_notes_length")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "author_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_visit_notes_org_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "visit_id"],
            ["visits.org_id", "visits.id"],
            name=op.f("fk_visit_notes_org_id_visits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_visit_notes_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_visit_notes")),
    )
    op.create_index(op.f("ix_visit_notes_visit_id"), "visit_notes", ["visit_id"], unique=False)
    # A rep's re-route is the rule's and the rep's, both; every other action one or the other.
    op.drop_constraint(
        op.f("ck_routing_decisions_rule_or_person"), "routing_decisions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_routing_decisions_rule_or_person"),
        "routing_decisions",
        "CASE WHEN action = 'reroute' THEN rule IS NOT NULL AND decided_by IS NOT NULL"
        " ELSE (rule IS NULL) <> (decided_by IS NULL) END",
    )


def downgrade() -> None:
    # The older rule refuses a row with both: such rows go first (R9), with the append-only
    # trigger off for that one statement.
    op.execute("ALTER TABLE routing_decisions DISABLE TRIGGER routing_decisions_never_change")
    op.execute("DELETE FROM routing_decisions WHERE rule IS NOT NULL AND decided_by IS NOT NULL")
    op.execute("ALTER TABLE routing_decisions ENABLE TRIGGER routing_decisions_never_change")
    op.drop_constraint(
        op.f("ck_routing_decisions_rule_or_person"), "routing_decisions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_routing_decisions_rule_or_person"),
        "routing_decisions",
        "(rule IS NULL) <> (decided_by IS NULL)",
    )
    op.drop_index(op.f("ix_visit_notes_visit_id"), table_name="visit_notes")
    op.drop_table("visit_notes")
    op.drop_index(op.f("ix_edits_visit_id"), table_name="edits")
    op.drop_table("edits")  # its trigger goes with it
