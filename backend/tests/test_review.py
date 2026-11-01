"""review.py: the rep's edits, added findings, notes and the confirm (batch 14)."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from countertalk import limits, reprocess, review
from countertalk.db import constraint_name, session_scope
from countertalk.errors import Conflict, Invalid, NotFound
from countertalk.models import (
    Edit,
    Evidence,
    Insight,
    RoutingDecision,
    Visit,
    VisitNote,
    VisitStatus,
    new_id,
)
from tests.conftest import give_transcript, lock_waiters
from tests.test_extraction import answer, insight, run, transcribed
from tests.test_quotes import PARAPHRASE
from tests.test_roles import ALL_PLACED, VISIT, tiny, tiny_org
from tests.test_routing import history_of, routes_of
from tests.test_show_quotes import BUSY
from tests.test_visits import Org_, load, make_org, new_visit
from tests.test_worker import wait_for

S = VisitStatus
PACK = tiny()  # payments: money, and field when high; supply: field; demand: no team
PAYMENT = insight(category="payments", severity="low", suggested_team="money")


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def reviewed(org: Org_, *found: dict[str, Any], busy: bool = False) -> tuple[str, list[str]]:
    """A visit in review with these insights: its ID and theirs, in order."""
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, answer(*(found or (PAYMENT,))), *((BUSY,) if busy else ()))
    with session_scope(read_only=True) as session:
        query = select(Insight.id).where(Insight.visit_id == visit_id).order_by(Insight.seq)
        return visit_id, list(session.scalars(query))


def edit(org: Org_, insight_id: str, **changes: str) -> Insight:
    with session_scope() as session:
        return review.edit_insight(
            session, lambda: PACK, org.org_id, org.rep_id, insight_id, **changes
        )


def edits(visit_id: str) -> list[tuple[str, str, Any, Any]]:
    with session_scope(read_only=True) as session:
        query = select(Edit.object, Edit.field, Edit.before, Edit.after)
        query = query.where(Edit.visit_id == visit_id).order_by(Edit.created_at, Edit.field)
        return [tuple(row) for row in session.execute(query)]


def code_of(call: Any, *args: Any, **kwargs: Any) -> str:
    with pytest.raises((Conflict, Invalid, NotFound)) as refused:
        call(*args, **kwargs)
    return refused.value.code


# --- editing ---------------------------------------------------------------------------------


def test_a_summary_edit_is_logged_and_doesnt_re_route(org: Org_) -> None:
    visit_id, [insight_id] = reviewed(org)

    edited = edit(org, insight_id, summary_en="  Two boxes, today.  ")

    assert edited.summary_en == "Two boxes, today."
    assert edits(visit_id) == [
        ("insight", "summary_en", "The shop wants two boxes.", "Two boxes, today.")
    ]
    assert [row[2] for row in history_of(insight_id)] == ["route"]  # no reroute


def test_a_category_change_moves_it_to_the_new_category_s_team(org: Org_) -> None:
    visit_id, [insight_id] = reviewed(org)
    assert routes_of(insight_id) == [(1, "money", "table")]

    edit(org, insight_id, category="supply")

    assert routes_of(insight_id) == [(1, "field", "table")]
    assert edits(visit_id) == [("insight", "category", "payments", "supply")]
    with session_scope(read_only=True) as session:
        rerouted = session.execute(
            select(RoutingDecision.action, RoutingDecision.rule, RoutingDecision.decided_by).where(
                RoutingDecision.insight_id == insight_id, RoutingDecision.action == "reroute"
            )
        ).all()
    assert [tuple(row) for row in rerouted] == [("reroute", "disagreed", org.rep_id)]  # AI: money


def test_a_severity_change_brings_in_the_override(org: Org_) -> None:
    _, [insight_id] = reviewed(org)

    edit(org, insight_id, severity="high")

    assert routes_of(insight_id) == [(1, "money", "table"), (2, "field", "override")]
    assert [row[3] for row in history_of(insight_id)][-2:] == ["agreed", "override:payments"]


def test_an_unchanged_value_writes_nothing(org: Org_) -> None:
    visit_id, [insight_id] = reviewed(org)

    edit(org, insight_id, category="payments", severity="low")

    assert edits(visit_id) == []
    assert len(history_of(insight_id)) == 1


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"category": "nonsense"}, "invalid_category"),
        ({"severity": "urgent"}, "invalid_severity"),
        ({"summary_en": "   "}, "summary_empty"),
        ({"summary_en": "x" * (limits.SUMMARY_MAX_CHARS + 1)}, "summary_too_long"),
    ],
)
def test_a_value_the_playbook_hasnt_got_is_refused(
    org: Org_, changes: dict[str, str], code: str
) -> None:
    visit_id, [insight_id] = reviewed(org)

    assert code_of(edit, org, insight_id, **changes) == code
    assert edits(visit_id) == []


def test_an_unverified_insight_can_be_deleted_but_not_edited(org: Org_) -> None:
    unfound = insight(evidence=[{"turn_id": 3, "quote": PARAPHRASE}])
    visit_id, [insight_id] = reviewed(org, unfound, busy=True)

    assert code_of(edit, org, insight_id, summary_en="x") == "insight_unverified"
    with session_scope() as session:
        review.delete_insight(session, org.org_id, org.rep_id, insight_id)
    assert edits(visit_id)[0][1] == "deleted"


def test_another_org_s_insight_is_404(org: Org_) -> None:
    _, [insight_id] = reviewed(org)
    other = make_org("Another Org")

    assert code_of(edit, other, insight_id, summary_en="x") == "insight_not_found"
    assert code_of(edit, org, "not-an-id", summary_en="x") == "insight_not_found"


def test_edits_wait_for_review_and_stop_at_confirm(org: Org_) -> None:
    processing, [first] = reviewed(org)
    with session_scope() as session:
        reprocess.reprocess(session, org.org_id, processing, "extract")  # back to transcribed
    confirmed, [second] = reviewed(org)
    confirm(org, confirmed)

    assert code_of(edit, org, first, summary_en="x") == "visit_not_in_review"
    assert code_of(edit, org, second, summary_en="x") == "visit_confirmed"
    assert code_of(note, org, confirmed, "x") == "visit_confirmed"
    assert code_of(add, org, confirmed, turn_id=1) == "visit_confirmed"


# --- deleting --------------------------------------------------------------------------------


def test_a_deleted_insight_goes_with_its_quotes_and_routes_and_its_history_stays(
    org: Org_,
) -> None:
    visit_id, [insight_id] = reviewed(org)

    with session_scope() as session:
        review.delete_insight(session, org.org_id, org.rep_id, insight_id)

    with session_scope(read_only=True) as session:
        assert session.get(Insight, insight_id) is None
        assert (
            session.scalars(select(Evidence).where(Evidence.insight_id == insight_id)).all() == []
        )
    assert routes_of(insight_id) == []
    assert len(history_of(insight_id)) == 1  # the routing history outlives it, by design
    assert edits(visit_id) == [
        (
            "insight",
            "deleted",
            {
                "kind": "product_request",
                "category": "payments",
                "severity": "low",
                "summary_en": "The shop wants two boxes.",
                "verified": True,
                "added_by_rep": False,
            },
            None,
        )
    ]


# --- adding ----------------------------------------------------------------------------------


def add(org: Org_, visit_id: str, **changes: Any) -> Insight:
    found = {
        "insight_id": new_id(),
        "turn_id": 1,
        "kind": "problem",
        "category": "payments",
        "severity": "high",
        "summary_en": "A payout is late.",
    } | changes
    with session_scope() as session:
        return review.add_insight(session, PACK, org.org_id, org.rep_id, visit_id, **found)


def test_an_added_finding_quotes_its_turn_exactly_and_is_routed_by_the_table(
    org: Org_,
) -> None:
    visit_id, _ = reviewed(org)

    added = add(org, visit_id)

    assert (added.seq, added.added_by_rep, added.verified, added.extraction_id) == (
        1,
        True,
        True,
        None,
    )
    with session_scope(read_only=True) as session:
        quote = session.scalars(select(Evidence).where(Evidence.insight_id == added.id)).one()
    # T1 is the shopkeeper's, at 4.0 s: playback starts 1.5 s before (D1).
    assert (quote.turn_id, quote.speaker_role, quote.match) == (1, "shopkeeper", "exact")
    assert (quote.quote_final, quote.char_start, quote.char_end, quote.start_ms) == (
        "Kurkure ke do dabbe chahiye.",
        0,
        28,
        2_500,
    )
    assert routes_of(added.id) == [(1, "money", "table"), (2, "field", "override")]
    assert [row[3] for row in history_of(added.id)] == ["table_only", "override:payments"]
    assert edits(visit_id) == [
        (
            "insight",
            "created",
            None,
            {
                "turn_id": 1,
                "kind": "problem",
                "category": "payments",
                "severity": "high",
                "summary_en": "A payout is late.",
            },
        )
    ]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"turn_id": 99}, "turn_not_found"),
        # Only the shopkeeper's words become insights: the rep's line, and a customer's.
        ({"turn_id": 0}, "not_shopkeeper_turn"),
        ({"turn_id": 2}, "not_shopkeeper_turn"),
        ({"kind": "gossip"}, "invalid_kind"),
        ({"category": "nonsense"}, "invalid_category"),
        ({"summary_en": ""}, "summary_empty"),
    ],
)
def test_an_added_finding_must_name_a_turn_of_this_visit_and_the_playbook_s_keys(
    org: Org_, changes: dict[str, Any], code: str
) -> None:
    visit_id, _ = reviewed(org)

    assert code_of(add, org, visit_id, **changes) == code
    assert edits(visit_id) == []


def test_a_turn_only_another_visit_has_is_404(org: Org_) -> None:
    visit_id, _ = reviewed(org)  # turns 0 to 3
    other = new_visit(org)
    give_transcript(other, [*VISIT, ("c0:B", 80_000, "Ek aur baat.")])  # it has a turn 4

    assert code_of(add, org, visit_id, turn_id=4) == "turn_not_found"


# --- notes -----------------------------------------------------------------------------------


def note(org: Org_, visit_id: str, body: str) -> VisitNote:
    with session_scope() as session:
        return review.add_note(session, org.org_id, org.rep_id, visit_id, new_id(), body)


def test_a_note_is_kept_for_the_record_and_never_routed(org: Org_) -> None:
    visit_id, _ = reviewed(org)
    with session_scope(read_only=True) as session:
        decisions_before = session.scalar(select(func.count()).select_from(RoutingDecision))

    added = note(org, visit_id, "  The owner was away; spoke to his son.  ")

    assert added.text == "The owner was away; spoke to his son."
    with session_scope(read_only=True) as session:
        assert session.scalar(select(func.count()).select_from(RoutingDecision)) == decisions_before


@pytest.mark.parametrize(
    ("body", "code"),
    [(" ", "note_empty"), ("x" * (limits.NOTE_MAX_CHARS + 1), "note_too_long")],
)
def test_a_note_is_one_to_note_max_chars(org: Org_, body: str, code: str) -> None:
    visit_id, _ = reviewed(org)

    assert code_of(note, org, visit_id, body) == code


def test_the_database_holds_the_note_length_too(org: Org_) -> None:
    visit_id, _ = reviewed(org)

    with pytest.raises(IntegrityError) as refused, session_scope() as session:
        session.add(
            VisitNote(
                org_id=org.org_id,
                visit_id=visit_id,
                author_id=org.rep_id,
                text="x" * (limits.NOTE_MAX_CHARS + 1),
            )
        )

    assert constraint_name(refused.value) == "ck_visit_notes_length"


# --- confirming ------------------------------------------------------------------------------


def confirm(org: Org_, visit_id: str) -> Visit:
    with session_scope() as session:
        return review.confirm(session, lambda: PACK, org.org_id, org.rep_id, visit_id)


def test_confirm_shows_the_verified_insights_and_locks_the_visit(org: Org_) -> None:
    unfound = insight(evidence=[{"turn_id": 3, "quote": PARAPHRASE}])
    visit_id, [shown, hidden] = reviewed(org, PAYMENT, unfound, busy=True)

    assert confirm(org, visit_id).status == S.CONFIRMED

    with session_scope(read_only=True) as session:
        visible = {i.id: i.visible for i in session.scalars(select(Insight))}
    assert visible == {shown: True, hidden: False}
    assert edits(visit_id) == [("visit", "status", "review", "confirmed")]


def test_confirming_twice_returns_the_visit_and_adds_nothing(org: Org_) -> None:
    visit_id, _ = reviewed(org)
    first = confirm(org, visit_id)

    second = confirm(org, visit_id)

    assert (second.id, second.status, second.updated_at) == (
        first.id,
        first.status,
        first.updated_at,
    )
    assert len(edits(visit_id)) == 1


def test_a_visit_still_processing_isnt_confirmed(org: Org_) -> None:
    assert code_of(confirm, org, transcribed(org)) == "visit_not_in_review"


def test_an_edit_waiting_on_a_confirm_finds_the_visit_confirmed(org: Org_) -> None:
    visit_id, [insight_id] = reviewed(org)

    with ThreadPoolExecutor(max_workers=1) as pool, session_scope() as session:
        review.confirm(session, lambda: PACK, org.org_id, org.rep_id, visit_id)  # its lock
        editing = pool.submit(code_of, edit, org, insight_id, summary_en="x")
        wait_for(lambda: lock_waiters() == 1, "the edit never waited for the visit's lock")
        session.commit()

        assert editing.result(timeout=10) == "visit_confirmed"
    assert load(visit_id).status == S.CONFIRMED
    assert [row[1] for row in edits(visit_id)] == ["status"]


# --- the log ---------------------------------------------------------------------------------


def test_the_edit_log_is_never_changed_and_goes_with_its_visit(org: Org_) -> None:
    visit_id, [insight_id] = reviewed(org)
    edit(org, insight_id, summary_en="x")

    for change in (update(Edit).values(field="y"), delete(Edit)):
        with pytest.raises(DBAPIError) as refused, session_scope() as session:
            session.execute(change)
        assert refused.value.orig.sqlstate == "23001"  # type: ignore[union-attr]

    with session_scope() as session:
        session.execute(delete(Visit).where(Visit.id == visit_id))
    with session_scope(read_only=True) as session:
        assert session.scalar(text("SELECT count(*) FROM edits")) == 0


def test_a_re_extraction_numbers_its_insights_after_a_rep_s_kept_one(org: Org_) -> None:
    visit_id, [ai] = reviewed(org)
    with session_scope() as session:
        review.delete_insight(session, org.org_id, org.rep_id, ai)
    added = add(org, visit_id)  # seq 0: the only insight left
    with session_scope() as session:
        reprocess.reprocess(session, org.org_id, visit_id, "extract")

    run(visit_id, answer())  # roles saved: extraction only

    with session_scope(read_only=True) as session:
        found = session.execute(
            select(Insight.seq, Insight.added_by_rep).where(Insight.visit_id == visit_id)
        )
        assert sorted(tuple(row) for row in found) == [(0, True), (1, False)]
    assert added.seq == 0


def test_a_re_route_keeps_the_unknown_ai_team_key(org: Org_) -> None:
    _, [insight_id] = reviewed(org, insight(category="payments", suggested_team="finance"))
    assert history_of(insight_id)[0][4] == "finance"  # another org's key, recorded

    edit(org, insight_id, category="supply")

    assert history_of(insight_id)[-1][2:] == ("reroute", "table_only", "finance")


@pytest.mark.parametrize(
    ("values", "constraint"),
    [
        ({"field": "created", "before": "x"}, "ck_edits_before"),
        ({"field": "summary_en", "before": None, "after": "x"}, "ck_edits_before"),
        ({"field": "deleted", "after": "x"}, "ck_edits_after"),
        ({"object": "visit", "field": "summary_en"}, "ck_edits_visit_status"),
    ],
)
def test_the_database_checks_each_edit_row(
    org: Org_, values: dict[str, Any], constraint: str
) -> None:
    visit_id, _ = reviewed(org)
    row = {
        "org_id": org.org_id,
        "visit_id": visit_id,
        "actor_id": org.rep_id,
        "object": "insight",
        "object_id": visit_id,
        "field": "summary_en",
        "before": "a",
        "after": "b",
    } | values

    with pytest.raises(IntegrityError) as refused, session_scope() as session:
        session.add(Edit(**row))

    assert constraint_name(refused.value) == constraint


def test_a_summary_edit_never_loads_the_org_s_pack(org: Org_) -> None:
    _, [insight_id] = reviewed(org)

    def unreadable() -> Any:
        raise AssertionError("the pack was loaded for a summary edit")

    with session_scope() as session:
        found = review.edit_insight(
            session, unreadable, org.org_id, org.rep_id, insight_id, summary_en="Two boxes."
        )

    assert found.summary_en == "Two boxes."


def test_adding_the_same_finding_again_adds_nothing(org: Org_) -> None:
    visit_id, _ = reviewed(org)
    insight_id = new_id()

    first = add(org, visit_id, insight_id=insight_id)
    again = add(org, visit_id, insight_id=insight_id)

    assert (again.id, again.seq) == (first.id, first.seq)
    assert [row[1] for row in edits(visit_id)] == ["created"]
    assert len(history_of(insight_id)) == 2  # routed once: money, and field (override)


def test_an_id_that_s_another_insight_s_is_refused(org: Org_) -> None:
    visit_id, [extracted] = reviewed(org)

    assert code_of(add, org, visit_id, insight_id=extracted) == "insight_exists"


def test_adding_the_same_note_again_adds_nothing(org: Org_) -> None:
    visit_id, _ = reviewed(org)
    note_id = new_id()

    with session_scope() as session:
        review.add_note(session, org.org_id, org.rep_id, visit_id, note_id, "Owner away.")
    with session_scope() as session:
        review.add_note(session, org.org_id, org.rep_id, visit_id, note_id, "Owner away.")

    with session_scope(read_only=True) as session:
        assert session.scalar(select(func.count()).select_from(VisitNote)) == 1
