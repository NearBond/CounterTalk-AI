"""insight routes and routing decisions

Revision ID: b2e9fce36da2
Revises: 3e9b5fc94d3a
Create Date: 2026-11-01 20:31:17.779820
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2e9fce36da2"
down_revision: str | Sequence[str] | None = "3e9b5fc94d3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # First: the target of insight_routes' (org_id, insight_id) key.
    op.create_unique_constraint(op.f("uq_insights_org_id_id"), "insights", ["org_id", "id"])
    op.create_table(
        "routing_decisions",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("insight_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=True),
        sa.Column("team_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("rule", sa.String(), nullable=True),
        sa.Column("decided_by", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("ai_team_key_unknown", sa.String(), nullable=True),
        sa.Column("extraction_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(slot IS NULL) = (team_id IS NULL)"
            " AND (team_id IS NULL) = COALESCE(rule = 'triage', false)",
            name=op.f("ck_routing_decisions_triage"),
        ),
        sa.CheckConstraint(
            "action IN ('route', 'override', 'reroute', 'accept', 'move', 'also_send',"
            " 'triage_assign')",
            name=op.f("ck_routing_decisions_action"),
        ),
        sa.CheckConstraint(
            "(rule IS NULL) <> (decided_by IS NULL)",
            name=op.f("ck_routing_decisions_rule_or_person"),
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"],
            ["extractions.id"],
            name=op.f("fk_routing_decisions_extraction_id_extractions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "decided_by"],
            ["users.org_id", "users.id"],
            name=op.f("fk_routing_decisions_org_id_users"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "team_id"],
            ["teams.org_id", "teams.id"],
            name=op.f("fk_routing_decisions_org_id_teams"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_routing_decisions_org_id_orgs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_decisions")),
    )
    op.create_index(
        op.f("ix_routing_decisions_extraction_id"),
        "routing_decisions",
        ["extraction_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_routing_decisions_insight_id"), "routing_decisions", ["insight_id"], unique=False
    )
    # Append-only, through batch 6's refuse_change(): a cascade from the extraction still goes.
    op.execute(
        "CREATE TRIGGER routing_decisions_never_change BEFORE UPDATE OR DELETE"
        " ON routing_decisions FOR EACH ROW EXECUTE FUNCTION refuse_change()"
    )
    op.create_table(
        "insight_routes",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("insight_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("routed_by", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "routed_by IN ('table', 'ai', 'override', 'person')",
            name=op.f("ck_insight_routes_routed_by"),
        ),
        sa.CheckConstraint("slot BETWEEN 1 AND 2", name=op.f("ck_insight_routes_slot")),
        sa.ForeignKeyConstraint(
            ["org_id", "insight_id"],
            ["insights.org_id", "insights.id"],
            name=op.f("fk_insight_routes_org_id_insights"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "team_id"],
            ["teams.org_id", "teams.id"],
            name=op.f("fk_insight_routes_org_id_teams"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_insight_routes_org_id_orgs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_insight_routes")),
        sa.UniqueConstraint("insight_id", "slot", name=op.f("uq_insight_routes_insight_id_slot")),
        sa.UniqueConstraint(
            "insight_id", "team_id", name=op.f("uq_insight_routes_insight_id_team_id")
        ),
    )
    op.create_index(op.f("ix_insight_routes_team_id"), "insight_routes", ["team_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_insight_routes_team_id"), table_name="insight_routes")
    op.drop_table("insight_routes")
    op.drop_index(op.f("ix_routing_decisions_insight_id"), table_name="routing_decisions")
    op.drop_index(op.f("ix_routing_decisions_extraction_id"), table_name="routing_decisions")
    op.drop_table("routing_decisions")  # its trigger goes with it
    op.drop_constraint(op.f("uq_insights_org_id_id"), "insights", type_="unique")
