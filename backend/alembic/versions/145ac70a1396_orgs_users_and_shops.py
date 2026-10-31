"""orgs, users and shops

Revision ID: 145ac70a1396
Revises:
Create Date: 2026-11-01 02:15:49.547760
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "145ac70a1396"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orgs",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orgs")),
        sa.UniqueConstraint("name", name=op.f("uq_orgs_name")),
    )
    op.create_table(
        "shops",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("area", sa.String(), nullable=False),
        sa.Column("do_not_record", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_shops_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shops")),
        sa.UniqueConstraint("org_id", "name", "area", name=op.f("uq_shops_org_id_name_area")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('rep', 'team_member', 'manager', 'admin')", name=op.f("ck_users_role")
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_users_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index(op.f("ix_users_org_id"), "users", ["org_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_org_id"), table_name="users")
    op.drop_table("users")
    op.drop_table("shops")
    op.drop_table("orgs")
