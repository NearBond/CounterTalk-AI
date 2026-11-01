"""routing.py: the AI suggests, code decides (batch 12; BRIEF D7)."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError

from countertalk import routing
from countertalk.db import session_scope
from countertalk.errors import Conflict, NotFound
from countertalk.models import (
    Extraction,
    Insight,
    InsightRoute,
    RoutingDecision,
    Team,
    Visit,
    new_id,
)
from countertalk.playbooks import ORG_PACKS_DIR, PLAYBOOKS_DIR, Override, Route, load_org_pack
from countertalk.routing import Choice, Decision
from tests.conftest import lock_waiters
from tests.test_extraction import transcribed
from tests.test_roles import tiny, tiny_org
from tests.test_visits import Org_, make_org
from tests.test_worker import wait_for

TINY = routing.table_of(tiny())  # payments: money, and field when high; supply: field
TEAMS = {"money", "field"}
TABLE, AI, OVERRIDE = "table", "ai", "override"


def choice(slot: int, team: str, routed_by: str, rule: str) -> Choice:
    return Choice(slot, team, routing.RoutedBy(routed_by), rule)


# (category, severity, the AI's team, the routes, the unknown key)
CASES = [
    ("payments", "low", "money", [(1, "money", TABLE, "agreed")], None),
    ("payments", "low", None, [(1, "money", TABLE, "table_only")], None),
    ("payments", "low", "field", [(1, "money", TABLE, "disagreed")], None),
    ("other", "low", "field", [(1, "field", AI, "ai_filled_gap")], None),
    ("other", "low", None, [], None),  # triage
    # Another org's team key is never used: the AI has no suggestion, and it's recorded.
    ("payments", "low", "finance", [(1, "money", TABLE, "table_only")], "finance"),
    ("other", "low", "finance", [], "finance"),
    # The override: at its severity or above, never below.
    (
        "payments",
        "high",
        "money",
        [(1, "money", TABLE, "agreed"), (2, "field", OVERRIDE, "override:payments")],
        None,
    ),
    (
        "payments",
        "high",
        "field",
        [(1, "money", TABLE, "disagreed"), (2, "field", OVERRIDE, "override:payments")],
        None,
    ),
    ("payments", "medium", None, [(1, "money", TABLE, "table_only")], None),
    ("payments", "urgent", None, [(1, "money", TABLE, "table_only")], None),  # not a severity
    ("no_such_category", "high", None, [], None),
]


@pytest.mark.parametrize(("category", "severity", "ai", "routes", "unknown"), CASES)
def test_the_rule(
    category: str,
    severity: str,
    ai: str | None,
    routes: list[tuple[int, str, str, str]],
    unknown: str | None,
) -> None:
    decided = routing.decide(category, severity, ai, TINY, TEAMS)

    assert decided == Decision(tuple(choice(*r) for r in routes), unknown)


def test_with_no_main_team_the_override_takes_slot_1_and_isnt_sent_twice() -> None:
    gap = routing.Table(
        {
            "quality": Route(
                "quality", None, Override(category="quality", min_severity="high", team="field")
            )
        },
        TINY.severities,
    )

    alone = routing.decide("quality", "high", None, gap, TEAMS)
    same = routing.decide("quality", "high", "field", gap, TEAMS)
    other = routing.decide("quality", "high", "money", gap, TEAMS)

    # The table decided: the AI agreeing doesn't make it a suggestion to accept.
    assert alone.routes == same.routes == (choice(1, "field", OVERRIDE, "override:quality"),)
    assert other.routes == (
        choice(1, "money", AI, "ai_filled_gap"),
        choice(2, "field", OVERRIDE, "override:quality"),
    )


def test_a_pack_team_the_org_hasnt_got_is_skipped_not_guessed() -> None:
    no_money = routing.decide("payments", "high", None, TINY, {"field"})
    no_field = routing.decide("payments", "high", "money", TINY, {"money"})

    assert no_money.routes == (choice(1, "field", OVERRIDE, "override:payments"),)
    assert no_field.routes == (choice(1, "money", TABLE, "agreed"),)


def test_each_org_routes_a_category_to_its_own_team() -> None:
    pepsico = load_org_pack("pepsico-india-demo", 1, ORG_PACKS_DIR, PLAYBOOKS_DIR)
    theirs = routing.decide("payments", "low", None, routing.table_of(pepsico), {"finance"})

    assert theirs.routes == (choice(1, "finance", TABLE, "table_only"),)
    assert routing.decide("payments", "low", None, TINY, TEAMS).routes[0].team == "money"


# --- the database ----------------------------------------------------------------------------


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def an_insight(org: Org_, *, verified: bool = True) -> str:
    """An extracted insight of a transcribed visit, not yet routed: its ID."""
    visit_id = transcribed(org)
    with session_scope() as session:
        visit = session.get_one(Visit, visit_id)
        extraction = Extraction(
            id=new_id(),
            visit_id=visit_id,
            transcript_id=visit.transcript_id,
            playbook_id="fmcg-kirana-visit",
            playbook_version=1,
            org_pack_id="tiny-demo",
            org_pack_version=1,
            model="test",
            prompt_hash="0",
            raw_json={},
        )
        session.add(extraction)
        session.flush()
        insight = Insight(
            id=new_id(),
            org_id=org.org_id,
            visit_id=visit_id,
            seq=0,
            extraction_id=extraction.id,
            kind="complaint",
            category="payments",
            severity="high",
            summary_en="A payout is late.",
            sku_mentions=[],
            competitor_mentions=[],
            verified=verified,
        )
        session.add(insight)
        return insight.id


def team_ids(org_id: str) -> dict[str, str]:
    with session_scope(read_only=True) as session:
        query = select(Team.key, Team.id).where(Team.org_id == org_id)
        return {key: team_id for key, team_id in session.execute(query)}


def route(insight_id: str, decision: Decision) -> bool:
    with session_scope() as session:
        insight = session.get_one(Insight, insight_id)
        return routing.route_new(session, insight, decision, team_ids(insight.org_id))


def routes_of(insight_id: str) -> list[tuple[int, str, str]]:
    with session_scope(read_only=True) as session:
        query = (
            select(InsightRoute.slot, Team.key, InsightRoute.routed_by)
            .join(Team, Team.id == InsightRoute.team_id)
            .where(InsightRoute.insight_id == insight_id)
            .order_by(InsightRoute.slot)
        )
        return [tuple(row) for row in session.execute(query)]


def history_of(insight_id: str) -> list[tuple[int | None, str | None, str, str | None, str | None]]:
    with session_scope(read_only=True) as session:
        query = (
            select(RoutingDecision.slot, Team.key, RoutingDecision.action, RoutingDecision.rule)
            .add_columns(RoutingDecision.ai_team_key_unknown)
            .outerjoin(Team, Team.id == RoutingDecision.team_id)
            .where(RoutingDecision.insight_id == insight_id)
            .order_by(RoutingDecision.slot.nulls_first(), RoutingDecision.created_at)
        )
        return [tuple(row) for row in session.execute(query)]


HIGH_PAYMENT = routing.decide("payments", "high", "finance", TINY, TEAMS)


def test_routing_writes_each_route_with_its_history(org: Org_) -> None:
    insight_id = an_insight(org)

    assert route(insight_id, HIGH_PAYMENT)

    assert routes_of(insight_id) == [(1, "money", "table"), (2, "field", "override")]
    assert history_of(insight_id) == [
        (1, "money", "route", "table_only", "finance"),  # the unknown key on slot 1 only
        (2, "field", "override", "override:payments", None),
    ]


def test_triage_is_one_history_row_and_no_route(org: Org_) -> None:
    insight_id = an_insight(org)

    assert route(insight_id, routing.decide("other", "low", "finance", TINY, TEAMS))

    assert routes_of(insight_id) == []
    assert history_of(insight_id) == [(None, None, "route", "triage", "finance")]


@pytest.mark.parametrize("decision", [HIGH_PAYMENT, Decision((), None)], ids=["routed", "triage"])
def test_routing_twice_adds_nothing(org: Org_, decision: Decision) -> None:
    insight_id = an_insight(org)
    route(insight_id, decision)
    before = (routes_of(insight_id), history_of(insight_id))

    assert not route(insight_id, decision)

    assert (routes_of(insight_id), history_of(insight_id)) == before


def test_an_unverified_insight_is_never_routed(org: Org_) -> None:
    insight_id = an_insight(org, verified=False)

    assert not route(insight_id, HIGH_PAYMENT)

    assert (routes_of(insight_id), history_of(insight_id)) == ([], [])


def test_the_history_is_never_changed_or_deleted(org: Org_) -> None:
    insight_id = an_insight(org)
    route(insight_id, HIGH_PAYMENT)

    for change in (
        update(RoutingDecision).values(rule="agreed"),
        delete(RoutingDecision),
    ):
        with pytest.raises(DBAPIError) as refused, session_scope() as session:
            session.execute(change.where(RoutingDecision.insight_id == insight_id))
        assert refused.value.orig.sqlstate == "23001"  # type: ignore[union-attr]  # restrict_violation


def test_a_replaced_insight_loses_its_routes_but_not_its_history(org: Org_) -> None:
    insight_id = an_insight(org)
    route(insight_id, HIGH_PAYMENT)

    with session_scope() as session:
        session.execute(delete(Insight).where(Insight.id == insight_id))

    assert routes_of(insight_id) == []
    assert len(history_of(insight_id)) == 2


def test_the_history_goes_with_its_visit(org: Org_) -> None:
    insight_id = an_insight(org)
    route(insight_id, HIGH_PAYMENT)

    with session_scope() as session:
        visit_id = session.get_one(Insight, insight_id).visit_id
        session.execute(delete(Visit).where(Visit.id == visit_id))

    assert history_of(insight_id) == []


# --- also send -------------------------------------------------------------------------------


def a_third_team(org: Org_) -> str:
    with session_scope() as session:
        team = Team(org_id=org.org_id, key="third", name="Third team")
        session.add(team)
        session.flush()
        return team.id


def send(org: Org_, insight_id: str, team_id: str) -> int:
    with session_scope() as session:
        return routing.also_send(session, org.org_id, insight_id, team_id, org.rep_id)


def routed_to_money(org: Org_) -> str:
    insight_id = an_insight(org)
    route(insight_id, routing.decide("payments", "low", "field", TINY, TEAMS))
    return insight_id


def test_also_send_fills_slot_2_with_its_history(org: Org_) -> None:
    insight_id = routed_to_money(org)

    assert send(org, insight_id, team_ids(org.org_id)["field"]) == 2

    assert routes_of(insight_id) == [(1, "money", "table"), (2, "field", "person")]
    assert history_of(insight_id)[-1] == (2, "field", "also_send", None, None)
    with session_scope(read_only=True) as session:
        decided = select(RoutingDecision.decided_by).where(RoutingDecision.action == "also_send")
        assert session.scalars(decided).all() == [org.rep_id]


def test_also_send_takes_the_first_free_slot(org: Org_) -> None:
    insight_id = an_insight(org)
    route(insight_id, HIGH_PAYMENT)
    with session_scope() as session:  # as a later change might leave it
        session.execute(
            delete(InsightRoute).where(
                InsightRoute.insight_id == insight_id, InsightRoute.slot == 1
            )
        )

    assert send(org, insight_id, team_ids(org.org_id)["money"]) == 1


def code_of(org: Org_, insight_id: str, team_id: str) -> str:
    with pytest.raises((Conflict, NotFound)) as refused:
        send(org, insight_id, team_id)
    return refused.value.code


def test_a_third_team_is_refused(org: Org_) -> None:
    insight_id = an_insight(org)
    route(insight_id, HIGH_PAYMENT)  # money and field

    assert code_of(org, insight_id, a_third_team(org)) == "team_limit"
    assert len(routes_of(insight_id)) == 2


def test_also_send_refuses_a_team_it_has_a_triage_insight_and_another_orgs(org: Org_) -> None:
    insight_id, in_triage = routed_to_money(org), an_insight(org)
    route(in_triage, Decision((), None))
    other = make_org("Another Org")
    with session_scope() as session:
        session.add(Team(org_id=other.org_id, key="money", name="Theirs"))
    theirs = team_ids(other.org_id)["money"]

    assert code_of(org, insight_id, team_ids(org.org_id)["money"]) == "already_routed"
    assert code_of(org, in_triage, team_ids(org.org_id)["field"]) == "not_routed"
    assert code_of(org, insight_id, theirs) == "team_not_found"
    assert code_of(other, insight_id, theirs) == "insight_not_found"
    assert code_of(org, "not-an-id", theirs) == "insight_not_found"


def test_two_also_sends_at_once_one_wins_and_the_other_gets_team_limit(org: Org_) -> None:
    insight_id = routed_to_money(org)
    field, third = team_ids(org.org_id)["field"], a_third_team(org)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        # The first holds the insight's lock, with slot 2 written but not committed.
        assert routing.also_send(session, org.org_id, insight_id, field, org.rep_id) == 2
        second = pool.submit(send, org, insight_id, third)
        wait_for(lambda: lock_waiters() == 1, "the second never waited for the insight's lock")
        session.commit()
        with pytest.raises(Conflict) as refused:
            second.result(timeout=10)

    assert refused.value.code == "team_limit"
    assert routes_of(insight_id) == [(1, "money", "table"), (2, "field", "person")]


def test_the_cap_is_the_one_constraint_routing_knows() -> None:
    with session_scope(read_only=True) as session:
        check = session.scalar(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
            {"name": routing.CAP_CONSTRAINT},
        )

    assert check == "CHECK (((slot >= 1) AND (slot <= 2)))"
