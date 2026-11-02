"""catalog edits: the Edit log's rows with no visit

Revision ID: c2e959e3aeb6
Revises: 1ef106efe195
Create Date: 2026-11-02 12:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2e959e3aeb6"
down_revision: str | Sequence[str] | None = "1ef106efe195"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG = "'catalog_item', 'catalog_alias', 'alias_review'"


def upgrade() -> None:
    # Autogenerate doesn't compare check constraints: these are changed by hand.
    op.drop_constraint(op.f("ck_edits_object"), "edits", type_="check")
    op.create_check_constraint(
        op.f("ck_edits_object"), "edits", f"object IN ('insight', 'visit', {CATALOG})"
    )
    op.alter_column("edits", "visit_id", existing_type=sa.Uuid(as_uuid=False), nullable=True)
    op.create_check_constraint(
        op.f("ck_edits_visit_or_catalog"), "edits", f"(visit_id IS NULL) = (object IN ({CATALOG}))"
    )


def downgrade() -> None:
    # The catalog's rows go with their kind: the old table has no place for them. The log is
    # append-only, so its trigger is set aside for this one DELETE.
    op.execute("ALTER TABLE edits DISABLE TRIGGER edits_never_change")
    op.execute("DELETE FROM edits WHERE visit_id IS NULL")  # the catalog's rows, and only those
    op.execute("ALTER TABLE edits ENABLE TRIGGER edits_never_change")
    op.drop_constraint(op.f("ck_edits_visit_or_catalog"), "edits", type_="check")
    op.alter_column("edits", "visit_id", existing_type=sa.Uuid(as_uuid=False), nullable=False)
    op.drop_constraint(op.f("ck_edits_object"), "edits", type_="check")
    op.create_check_constraint(op.f("ck_edits_object"), "edits", "object IN ('insight', 'visit')")
