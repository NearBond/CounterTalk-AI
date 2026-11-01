"""The extract job links each insight to the org's catalog and routes each verified one, in the
same transaction as its rows (batch 12)."""

import logging

import pytest
from sqlalchemy import select

from countertalk import entities, extraction
from countertalk.db import session_scope
from countertalk.models import AliasReview, CatalogItem, Insight, InsightRoute, RoutingDecision
from tests.conftest import open_transactions
from tests.test_extraction import answer, insight, run, transcribed
from tests.test_quotes import PARAPHRASE
from tests.test_roles import ALL_PLACED, tiny_org
from tests.test_routing import history_of, routes_of
from tests.test_show_quotes import BUSY
from tests.test_visits import Org_


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def linked(visit_id: str) -> list[tuple[str, str | None, str | None, str | None]]:
    """Each insight's ID, its SKU's and competitor's catalog keys, and its scheme."""
    with session_scope(read_only=True) as session:
        query = select(CatalogItem.id, CatalogItem.key)
        keys = {item_id: key for item_id, key in session.execute(query)}
        query = select(Insight).where(Insight.visit_id == visit_id).order_by(Insight.seq)
        return [
            (i.id, keys.get(i.sku_id), keys.get(i.competitor_id), i.scheme_key)
            for i in session.scalars(query)
        ]


def reviews(visit_id: str) -> list[tuple[str, str]]:
    with session_scope(read_only=True) as session:
        query = select(AliasReview.kind, AliasReview.text).where(AliasReview.visit_id == visit_id)
        return sorted(tuple(row) for row in session.execute(query))


HIGH_PAYMENT = insight(
    category="payments",
    severity="high",
    sku_mentions=["Kurkure", "क्रंची"],
    competitor_mentions=["Rival"],
    scheme_key="crunchy_winter",
    suggested_team="field",
)


def test_each_insight_is_linked_and_routed_with_its_rows(
    org: Org_, caplog: pytest.LogCaptureFixture
) -> None:
    visit_id = transcribed(org)

    with caplog.at_level(logging.INFO, logger=extraction.__name__):
        run(visit_id, ALL_PLACED, answer(HIGH_PAYMENT))

    [(insight_id, sku, competitor, scheme)] = linked(visit_id)
    assert (sku, competitor, scheme) == ("crunchy", "rival", "crunchy_winter")
    assert reviews(visit_id) == [("own", "Kurkure")]  # in the tiny pack's catalog, no Kurkure
    assert routes_of(insight_id) == [(1, "money", "table"), (2, "field", "override")]
    assert history_of(insight_id) == [
        (1, "money", "route", "disagreed", None),
        (2, "field", "override", "override:payments", None),
    ]
    logged = [r.getMessage() for r in caplog.records if r.name == extraction.__name__]
    assert logged[-2:] == [
        f"linked visit={visit_id} exact=2 fuzzy=0 unknown=1 ambiguous=0 wrong_kind=0"
        " new_reviews=1 scheme_by_alias=0 scheme_dropped=0",
        f"routed visit={visit_id} routed=1 triage=0 unverified=0",
    ]


def test_an_unverified_insight_is_linked_but_never_routed(org: Org_) -> None:
    visit_id = transcribed(org)
    unfound = insight(evidence=[{"turn_id": 3, "quote": PARAPHRASE}], sku_mentions=["Crunchy"])

    run(visit_id, ALL_PLACED, answer(unfound), BUSY)  # its repair isn't answered

    [(insight_id, sku, _, _)] = linked(visit_id)
    assert sku == "crunchy"
    assert (routes_of(insight_id), history_of(insight_id)) == ([], [])


def test_extracting_again_routes_the_new_insights_and_reviews_a_name_once(org: Org_) -> None:
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, answer())  # demand: no table team, so the AI's field
    [(first, *_)] = linked(visit_id)
    with session_scope() as session:
        extraction.queue_again(session, org.org_id, visit_id)

    run(visit_id, answer())

    [(second, *_)] = linked(visit_id)
    assert routes_of(first) == []  # went with its insight
    assert routes_of(second) == [(1, "field", "ai")]
    assert [row[3] for row in history_of(first) + history_of(second)] == ["ai_filled_gap"] * 2
    assert reviews(visit_id) == [("own", "Kurkure")]
    with session_scope(read_only=True) as session:
        assert len(session.scalars(select(InsightRoute.id)).all()) == 1
        assert len(session.scalars(select(RoutingDecision.id)).all()) == 2


@pytest.mark.parametrize(
    ("given", "kept", "shown"),
    [
        ("winter scheme", "crunchy_winter", None),  # an alias: the key is kept
        ("lays_display_rack", None, "lays_display_rack"),  # another org's
        ("Diwali offer!", None, "(not a key)"),  # never other text in the log
    ],
)
def test_a_scheme_key_not_in_the_pack_is_matched_or_dropped_and_logged(
    org: Org_, caplog: pytest.LogCaptureFixture, given: str, kept: str | None, shown: str | None
) -> None:
    visit_id = transcribed(org)

    with caplog.at_level(logging.INFO, logger=extraction.__name__):
        run(visit_id, ALL_PLACED, answer(insight(scheme_key=given)))

    [(_, _, _, scheme)] = linked(visit_id)
    assert scheme == kept
    dropped = [r.getMessage() for r in caplog.records if r.getMessage().startswith("scheme")]
    assert dropped == (
        [] if shown is None else [f"scheme dropped visit={visit_id} insight=0 key={shown}"]
    )


def test_names_are_matched_with_no_transaction_open(
    org: Org_, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []
    match = entities.match

    def watched(text: str, names: entities.Names) -> entities.Found:
        seen.append(open_transactions())
        return match(text, names)

    monkeypatch.setattr(entities, "match", watched)

    run(transcribed(org), ALL_PLACED, answer(HIGH_PAYMENT))

    assert seen == [0, 0, 0]  # each mention; the visit's lock isn't held, nor any transaction
