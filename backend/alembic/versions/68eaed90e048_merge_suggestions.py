"""merge suggestions

Revision ID: 68eaed90e048
Revises: d8f1c2bfc9b0
Create Date: 2026-11-02 06:00:32.632053
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "68eaed90e048"
down_revision: str | Sequence[str] | None = "d8f1c2bfc9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fields(allowed: str) -> None:
    # Autogenerate doesn't compare check constraints: this one is changed by hand.
    op.drop_constraint(op.f("ck_issue_events_field"), "issue_events", type_="check")
    op.create_check_constraint(
        op.f("ck_issue_events_field"), "issue_events", f"field IN ({allowed})"
    )


def upgrade() -> None:
    # The target of a suggestion's (org_id, summary_id).
    op.create_unique_constraint(op.f("uq_summaries_org_id_id"), "summaries", ["org_id", "id"])
    op.create_table(
        "issue_merge_suggestions",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("team_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("into_issue_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("merge_issue_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("pair_key", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("summary_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("decided_by_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'open') = (decided_by_id IS NULL)"
            " AND (decided_by_id IS NULL) = (decided_at IS NULL)",
            name=op.f("ck_issue_merge_suggestions_decided"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'merged', 'dismissed')",
            name=op.f("ck_issue_merge_suggestions_status"),
        ),
        sa.CheckConstraint(
            "into_issue_id <> merge_issue_id", name=op.f("ck_issue_merge_suggestions_two_issues")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "decided_by_id"],
            ["users.org_id", "users.id"],
            name="fk_issue_merge_suggestions_decided_by_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "team_id", "into_issue_id"],
            ["issues.org_id", "issues.team_id", "issues.id"],
            name="fk_issue_merge_suggestions_into_issue_id_issues",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "team_id", "merge_issue_id"],
            ["issues.org_id", "issues.team_id", "issues.id"],
            name="fk_issue_merge_suggestions_merge_issue_id_issues",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_issue_merge_suggestions_org_id_orgs")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "summary_id"],
            ["summaries.org_id", "summaries.id"],
            name="fk_issue_merge_suggestions_summary_id_summaries",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_issue_merge_suggestions")),
        sa.UniqueConstraint(
            "org_id", "pair_key", name=op.f("uq_issue_merge_suggestions_org_id_pair_key")
        ),
    )
    op.create_index(
        op.f("ix_issue_merge_suggestions_into_issue_id"),
        "issue_merge_suggestions",
        ["into_issue_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_issue_merge_suggestions_merge_issue_id"),
        "issue_merge_suggestions",
        ["merge_issue_id"],
        unique=False,
    )
    _fields("'status', 'owner', 'due_at', 'merge'")


def downgrade() -> None:
    # The merge events go with their field: the old check would refuse them. issue_events is
    # append-only (its trigger refuses DELETE), so the trigger is set aside for this one delete.
    op.execute("ALTER TABLE issue_events DISABLE TRIGGER issue_events_never_change")
    op.execute("DELETE FROM issue_events WHERE field = 'merge'")
    op.execute("ALTER TABLE issue_events ENABLE TRIGGER issue_events_never_change")
    _fields("'status', 'owner', 'due_at'")
    op.drop_index(
        op.f("ix_issue_merge_suggestions_merge_issue_id"), table_name="issue_merge_suggestions"
    )
    op.drop_index(
        op.f("ix_issue_merge_suggestions_into_issue_id"), table_name="issue_merge_suggestions"
    )
    op.drop_table("issue_merge_suggestions")
    op.drop_constraint(op.f("uq_summaries_org_id_id"), "summaries", type_="unique")
