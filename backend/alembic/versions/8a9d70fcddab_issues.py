"""issues

Revision ID: 8a9d70fcddab
Revises: 986b04c6ade7
Create Date: 2026-11-02 00:59:43.383329
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8a9d70fcddab"
down_revision: str | Sequence[str] | None = "986b04c6ade7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTIVE = "status IN ('open', 'in_progress')"


def upgrade() -> None:
    op.create_table(
        "issues",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("team_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("issue_key", sa.String(), nullable=False),
        sa.Column("key_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("title_en", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("owner_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.String(), nullable=True),
        sa.Column("reopened_count", sa.Integer(), nullable=False),
        sa.Column("merged_into_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'merged') = (merged_into_id IS NOT NULL)"
            " AND (merged_into_id IS NULL OR merged_into_id <> id)",
            name=op.f("ck_issues_merged"),
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (resolution_note IS NOT NULL)",
            name=op.f("ck_issues_resolution_note"),
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (resolved_at IS NOT NULL)", name=op.f("ck_issues_resolved_at")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'resolved', 'merged')", name=op.f("ck_issues_status")
        ),
        sa.CheckConstraint(
            "char_length(resolution_note) BETWEEN 1 AND 2000",
            name=op.f("ck_issues_resolution_note_length"),
        ),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name=op.f("ck_issues_seen")),
        sa.CheckConstraint("version >= 1 AND reopened_count >= 0", name=op.f("ck_issues_counts")),
        sa.ForeignKeyConstraint(
            ["org_id", "merged_into_id"],
            ["issues.org_id", "issues.id"],
            name="fk_issues_merged_into_id_issues",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_id"], ["users.org_id", "users.id"], name="fk_issues_owner_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "team_id"], ["teams.org_id", "teams.id"], name=op.f("fk_issues_org_id_teams")
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_issues_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_issues")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_issues_org_id_id")),
        sa.UniqueConstraint("org_id", "team_id", "id", name=op.f("uq_issues_org_id_team_id_id")),
        sa.UniqueConstraint("seq", name=op.f("uq_issues_seq")),
    )
    op.create_index(
        "uq_issues_active_key",
        "issues",
        ["org_id", "team_id", "issue_key"],
        unique=True,
        postgresql_where=sa.text(ACTIVE),
    )
    op.create_table(
        "issue_events",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("issue_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("actor_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("from_value", sa.String(), nullable=True),
        sa.Column("to_value", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "field IN ('status', 'owner', 'due_at')", name=op.f("ck_issue_events_field")
        ),
        sa.CheckConstraint(
            "from_value IS DISTINCT FROM to_value", name=op.f("ck_issue_events_changed")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "actor_id"],
            ["users.org_id", "users.id"],
            name=op.f("fk_issue_events_org_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "issue_id"],
            ["issues.org_id", "issues.id"],
            name=op.f("fk_issue_events_org_id_issues"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_issue_events_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_issue_events")),
    )
    op.create_index(op.f("ix_issue_events_issue_id"), "issue_events", ["issue_id"], unique=False)
    # Append-only, through batch 6's refuse_change().
    op.execute(
        "CREATE TRIGGER issue_events_never_change BEFORE UPDATE OR DELETE"
        " ON issue_events FOR EACH ROW EXECUTE FUNCTION refuse_change()"
    )
    op.add_column("insight_routes", sa.Column("issue_id", sa.Uuid(as_uuid=False), nullable=True))
    op.create_index(
        op.f("ix_insight_routes_issue_id"), "insight_routes", ["issue_id"], unique=False
    )
    op.create_foreign_key(
        "fk_insight_routes_issue_id_issues",
        "insight_routes",
        "issues",
        ["org_id", "team_id", "issue_id"],
        ["org_id", "team_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_insight_routes_issue_id_issues", "insight_routes", type_="foreignkey")
    op.drop_index(op.f("ix_insight_routes_issue_id"), table_name="insight_routes")
    op.drop_column("insight_routes", "issue_id")
    # The trigger goes with its table.
    op.drop_index(op.f("ix_issue_events_issue_id"), table_name="issue_events")
    op.drop_table("issue_events")
    op.drop_index("uq_issues_active_key", table_name="issues", postgresql_where=sa.text(ACTIVE))
    op.drop_table("issues")
