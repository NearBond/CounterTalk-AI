"""catalog, teams and distributors

Revision ID: da6c5c626cf2
Revises: b05ce4d2c586
Create Date: 2026-11-01 14:21:30.050546
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "da6c5c626cf2"
down_revision: str | Sequence[str] | None = "b05ce4d2c586"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_items",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("company", sa.String(), nullable=False),
        sa.Column("brand", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('own', 'competitor')", name=op.f("ck_catalog_items_kind")),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_catalog_items_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_items")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_catalog_items_org_id_id")),
        sa.UniqueConstraint("org_id", "key", name=op.f("uq_catalog_items_org_id_key")),
    )
    op.create_table(
        "distributors",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("area", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_distributors_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_distributors")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_distributors_org_id_id")),
        sa.UniqueConstraint("org_id", "key", name=op.f("uq_distributors_org_id_key")),
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_teams_org_id_orgs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("org_id", "id", name=op.f("uq_teams_org_id_id")),
        sa.UniqueConstraint("org_id", "key", name=op.f("uq_teams_org_id_key")),
    )
    op.create_table(
        "catalog_aliases",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("item_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('pack', 'review', 'admin')", name=op.f("ck_catalog_aliases_source")
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "item_id"],
            ["catalog_items.org_id", "catalog_items.id"],
            name=op.f("fk_catalog_aliases_org_id_catalog_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["orgs.id"], name=op.f("fk_catalog_aliases_org_id_orgs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_aliases")),
        sa.UniqueConstraint("item_id", "text", name=op.f("uq_catalog_aliases_item_id_text")),
    )
    op.create_index(op.f("ix_catalog_aliases_org_id"), "catalog_aliases", ["org_id"], unique=False)
    op.add_column("orgs", sa.Column("org_pack_id", sa.String(), nullable=True))
    op.add_column("orgs", sa.Column("org_pack_version", sa.Integer(), nullable=True))
    op.add_column("shops", sa.Column("distributor_id", sa.Uuid(as_uuid=False), nullable=True))
    op.add_column("shops", sa.Column("beat", sa.String(), nullable=True))
    op.create_index(op.f("ix_shops_distributor_id"), "shops", ["distributor_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_shops_org_id_distributors"),
        "shops",
        "distributors",
        ["org_id", "distributor_id"],
        ["org_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_shops_org_id_distributors"), "shops", type_="foreignkey")
    op.drop_index(op.f("ix_shops_distributor_id"), table_name="shops")
    op.drop_column("shops", "beat")
    op.drop_column("shops", "distributor_id")
    op.drop_column("orgs", "org_pack_version")
    op.drop_column("orgs", "org_pack_id")
    op.drop_index(op.f("ix_catalog_aliases_org_id"), table_name="catalog_aliases")
    op.drop_table("catalog_aliases")
    op.drop_table("teams")
    op.drop_table("distributors")
    op.drop_table("catalog_items")
