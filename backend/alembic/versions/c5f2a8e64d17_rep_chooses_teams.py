"""the rep chooses each finding's team in review: routed_by rep, and the rep_choice action

Revision ID: c5f2a8e64d17
Revises: a3d9e71c5b20
Create Date: 2026-11-04 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c5f2a8e64d17"
down_revision: str | Sequence[str] | None = "a3d9e71c5b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROUTED_BY = "'table', 'ai', 'override', 'person'"
ACTIONS = "'route', 'override', 'reroute', 'accept', 'move', 'also_send', 'triage_assign'"
RULE_OR_PERSON = (
    "CASE WHEN action = 'reroute' THEN rule IS NOT NULL AND decided_by IS NOT NULL"
    " ELSE (rule IS NULL) <> (decided_by IS NULL) END"
)
REP_CHOICE = (
    "CASE WHEN action = 'reroute' THEN rule IS NOT NULL AND decided_by IS NOT NULL"
    " WHEN action = 'rep_choice' THEN decided_by IS NOT NULL"
    " AND (rule IS NULL OR rule = 'triage')"
    " ELSE (rule IS NULL) <> (decided_by IS NULL) END"
)


def _checks(routed_by: str, actions: str, rule_or_person: str) -> None:
    # Autogenerate doesn't compare check constraints: these are changed by hand.
    for name, table, text in (
        ("ck_insight_routes_routed_by", "insight_routes", f"routed_by IN ({routed_by})"),
        ("ck_routing_decisions_action", "routing_decisions", f"action IN ({actions})"),
        ("ck_routing_decisions_rule_or_person", "routing_decisions", rule_or_person),
    ):
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, text)


def upgrade() -> None:
    _checks(f"{ROUTED_BY}, 'rep'", f"{ACTIONS}, 'rep_choice'", REP_CHOICE)


def downgrade() -> None:
    # The older checks would refuse a rep's routes and choices: their routes become a person's,
    # and their history rows go (append-only, so the trigger is set aside for it).
    op.execute("UPDATE insight_routes SET routed_by = 'person' WHERE routed_by = 'rep'")
    op.execute("ALTER TABLE routing_decisions DISABLE TRIGGER routing_decisions_never_change")
    op.execute("DELETE FROM routing_decisions WHERE action = 'rep_choice'")
    op.execute("ALTER TABLE routing_decisions ENABLE TRIGGER routing_decisions_never_change")
    _checks(ROUTED_BY, ACTIONS, RULE_OR_PERSON)
