"""sign-in: users' team and password, sessions, sign-in failures

Revision ID: 1ef106efe195
Revises: b17cde2786c4
Create Date: 2026-11-02 09:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1ef106efe195"
down_revision: str | Sequence[str] | None = "b17cde2786c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("team_id", sa.Uuid(as_uuid=False), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_users_team_id_teams", "users", "teams", ["org_id", "team_id"], ["org_id", "id"]
    )
    # Team members made before sign-in have no team. Each one's email starts with its team's
    # key and a dash, as scripts/_k6.py names them ("finance-…@example.com"): the team is the
    # one in their own org with that key, the longest if two keys fit ("sales" and
    # "sales-north"). Never a default team: anyone left over stops this.
    op.execute(
        "UPDATE users SET team_id = ("
        "SELECT teams.id FROM teams WHERE teams.org_id = users.org_id"
        " AND starts_with(users.email, teams.key || '-')"
        " ORDER BY length(teams.key) DESC LIMIT 1)"
        " WHERE users.role = 'team_member'"
    )
    left = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM users WHERE role = 'team_member' AND team_id IS NULL")
    )
    if left:
        raise RuntimeError(
            f"{left} team member(s) have no team: their email doesn't start with a team key of"
            " their org and a dash. Set users.team_id for them, then upgrade again."
        )
    op.create_check_constraint(
        op.f("ck_users_team_member_team"),
        "users",
        "(role = 'team_member') = (team_id IS NOT NULL)",
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("org_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_sessions_expires_after_created")
        ),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_sessions_token_hash_sha256")
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], name=op.f("fk_sessions_org_id_orgs")),
        sa.ForeignKeyConstraint(
            ["org_id", "user_id"],
            ["users.org_id", "users.id"],
            name="fk_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index(op.f("ix_sessions_user_id"), "sessions", ["user_id"], unique=False)
    op.create_table(
        "signin_failures",
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_signin_failures_email_lowercase")),
        sa.CheckConstraint("failures > 0", name=op.f("ck_signin_failures_failures_positive")),
        sa.PrimaryKeyConstraint("email", name=op.f("pk_signin_failures")),
    )


def downgrade() -> None:
    op.drop_table("signin_failures")
    op.drop_index(op.f("ix_sessions_user_id"), table_name="sessions")
    op.drop_table("sessions")
    op.drop_constraint(op.f("ck_users_team_member_team"), "users", type_="check")
    op.drop_constraint("fk_users_team_id_teams", "users", type_="foreignkey")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "team_id")
