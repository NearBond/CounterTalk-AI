"""order items

Revision ID: 986b04c6ade7
Revises: e056cd0ce614
Create Date: 2026-11-02 00:03:46.907115
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "986b04c6ade7"
down_revision: str | Sequence[str] | None = "e056cd0ce614"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "order_items",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("visit_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("extraction_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("quantity", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("sku_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.CheckConstraint("char_length(name) > 0", name=op.f("ck_order_items_name_said")),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_order_items_quantity_positive")),
        sa.ForeignKeyConstraint(
            ["org_id", "sku_id"],
            ["catalog_items.org_id", "catalog_items.id"],
            name="fk_order_items_sku_id_catalog_items",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "visit_id"],
            ["visits.org_id", "visits.id"],
            name=op.f("fk_order_items_org_id_visits"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_order_items_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["visit_id", "extraction_id"],
            ["extractions.visit_id", "extractions.id"],
            name="fk_order_items_extraction_id_extractions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
        sa.UniqueConstraint("visit_id", "seq", name=op.f("uq_order_items_visit_id_seq")),
    )


def downgrade() -> None:
    op.drop_table("order_items")
