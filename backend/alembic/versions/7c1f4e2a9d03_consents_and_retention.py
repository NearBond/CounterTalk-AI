"""consents, consent links, and when a visit was confirmed and its audio deleted

Revision ID: 7c1f4e2a9d03
Revises: c2e959e3aeb6
Create Date: 2026-11-03 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c1f4e2a9d03"
down_revision: str | Sequence[str] | None = "c2e959e3aeb6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consents",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("shop_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("notice_version", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("recorded_by", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('yes', 'no')", name=op.f("ck_consents_decision")),
        sa.CheckConstraint("method IN ('link', 'paper')", name=op.f("ck_consents_method")),
        sa.CheckConstraint("language IN ('hi', 'en', 'mr')", name=op.f("ck_consents_language")),
        sa.CheckConstraint("notice_version >= 1", name=op.f("ck_consents_notice_version")),
        sa.CheckConstraint(
            "(method = 'paper') = (recorded_by IS NOT NULL)", name=op.f("ck_consents_recorded_by")
        ),
        sa.CheckConstraint(
            "method <> 'paper' OR decision = 'yes'", name=op.f("ck_consents_paper_is_yes")
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_consents_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["org_id", "shop_id"],
            ["shops.org_id", "shops.id"],
            name=op.f("fk_consents_org_id_shops"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "recorded_by"],
            ["users.org_id", "users.id"],
            name=op.f("fk_consents_org_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consents")),
        sa.UniqueConstraint("seq", name=op.f("uq_consents_seq")),
    )
    op.create_index("ix_consents_shop_id_seq", "consents", ["shop_id", "seq"], unique=False)
    # A shop's answers are its history: append-only, through batch 6's refuse_change().
    op.execute(
        "CREATE TRIGGER consents_never_change BEFORE UPDATE OR DELETE"
        " ON consents FOR EACH ROW EXECUTE FUNCTION refuse_change()"
    )
    op.create_table(
        "consent_links",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("shop_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_consent_links_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["org_id", "shop_id"],
            ["shops.org_id", "shops.id"],
            name=op.f("fk_consent_links_org_id_shops"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "created_by"],
            ["users.org_id", "users.id"],
            name=op.f("fk_consent_links_org_id_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_links")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_consent_links_token_hash")),
    )
    op.create_index(op.f("ix_consent_links_shop_id"), "consent_links", ["shop_id"], unique=False)

    op.add_column("visits", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "visits", sa.Column("audio_deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    # A visit confirmed before this: when the Edit log says it was confirmed, or its last
    # update if the log has no such row.
    op.execute(
        "UPDATE visits SET confirmed_at = COALESCE("
        "(SELECT min(e.created_at) FROM edits e WHERE e.object = 'visit'"
        " AND e.object_id = visits.id AND e.field = 'status' AND e.after #>> '{}' = 'confirmed'),"
        " updated_at) WHERE status = 'confirmed'"
    )
    op.create_check_constraint(
        op.f("ck_visits_confirmed_at"),
        "visits",
        "(status = 'confirmed') = (confirmed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_visits_audio_deleted_at"),
        "visits",
        "audio_deleted_at IS NULL OR status = 'confirmed'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_visits_audio_deleted_at"), "visits", type_="check")
    op.drop_constraint(op.f("ck_visits_confirmed_at"), "visits", type_="check")
    op.drop_column("visits", "audio_deleted_at")
    op.drop_column("visits", "confirmed_at")
    op.drop_index(op.f("ix_consent_links_shop_id"), table_name="consent_links")
    op.drop_table("consent_links")
    op.execute("DROP TRIGGER consents_never_change ON consents")
    op.drop_index("ix_consents_shop_id_seq", table_name="consents")
    op.drop_table("consents")
