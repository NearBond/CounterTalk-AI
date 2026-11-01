"""The visit's order items (batch 14d): what the shop orders on the visit, as said, saved with
the extraction's other rows and linked to the org's catalog. Never an insight, never routed."""

import logging
from decimal import Decimal

import pytest
from sqlalchemy import select

from countertalk import extraction, reprocess
from countertalk.db import session_scope
from countertalk.models import CatalogItem, Insight, OrderItem, Report
from tests.test_extract_links import reviews
from tests.test_extraction import answer, run, transcribed
from tests.test_roles import ALL_PLACED, tiny_org
from tests.test_visits import Org_


@pytest.fixture
def org() -> Org_:
    return tiny_org()


def item(name: str, quantity: float | None = 2, unit: str | None = "dabbe") -> dict[str, object]:
    return {"name": name, "quantity": quantity, "unit": unit}


def order_of(visit_id: str) -> list[tuple[int, str, Decimal | None, str | None, str | None]]:
    """Each item's seq, name, quantity, unit and catalog key."""
    with session_scope(read_only=True) as session:
        keys = dict(session.execute(select(CatalogItem.id, CatalogItem.key)).all())
        query = select(OrderItem).where(OrderItem.visit_id == visit_id).order_by(OrderItem.seq)
        return [
            (i.seq, i.name, i.quantity, i.unit, keys.get(i.sku_id) if i.sku_id else None)
            for i in session.scalars(query)
        ]


def test_the_order_is_saved_as_said_and_linked_to_the_catalog(
    org: Org_, caplog: pytest.LogCaptureFixture
) -> None:
    visit_id = transcribed(org)
    order = [item("क्रंची सॉल्टेड"), item("Zingo", None, None), item("Rival", 1.5, "kg")]

    with caplog.at_level(logging.INFO, logger=extraction.__name__):
        run(visit_id, ALL_PLACED, answer(order_items=order))

    assert order_of(visit_id) == [
        (0, "क्रंची सॉल्टेड", Decimal(2), "dabbe", "crunchy_salt"),
        # A name the catalog hasn't got goes to review, never to the nearest guess.
        (1, "Zingo", None, None, None),
        # A competitor's brand is no SKU of the org's: unlinked, and not a name to review.
        (2, "Rival", Decimal("1.5"), "kg", None),
    ]
    # Zingo to review with the insight's Kurkure; Rival, a competitor's, not.
    assert reviews(visit_id) == [("own", "Kurkure"), ("own", "Zingo")]
    with session_scope(read_only=True) as session:
        # The table holds them: the report's own JSON doesn't repeat them.
        assert session.scalars(select(Report.json)).one() == {
            "summary_en": "A routine order.",
            "next_visit_date": "2026-11-22",
        }
        # Never an insight: the answer's one insight is all there is.
        assert len(session.scalars(select(Insight)).all()) == 1
    logged = [r.getMessage() for r in caplog.records if r.name == extraction.__name__]
    assert [line for line in logged if line.startswith("order ")] == [
        f"order visit={visit_id} items=3 linked=1 unknown=1 ambiguous=0 not_own=1 dropped=0"
    ]


def test_a_quantity_that_isnt_a_count_is_left_out_and_a_blank_name_dropped(
    org: Org_, caplog: pytest.LogCaptureFixture
) -> None:
    visit_id = transcribed(org)
    order = [
        item("Crunchy", 0),
        item("Crunchy", -3, " "),
        item("  ", 4),
        item("Crunchy", 0.25, "box"),
        item(" Crunchy ", 12, " peti "),
    ]

    with caplog.at_level(logging.INFO, logger=extraction.__name__):
        run(visit_id, ALL_PLACED, answer(order_items=order))

    assert order_of(visit_id) == [
        (0, "Crunchy", None, "dabbe", "crunchy"),
        (1, "Crunchy", None, None, "crunchy"),
        (2, "Crunchy", Decimal("0.25"), "box", "crunchy"),
        (3, "Crunchy", Decimal(12), "peti", "crunchy"),
    ]
    # Every item said is in one bucket: the blank one too.
    logged = [r.getMessage() for r in caplog.records if r.name == extraction.__name__]
    assert [line for line in logged if line.startswith("order ")] == [
        f"order visit={visit_id} items=5 linked=4 unknown=0 ambiguous=0 not_own=0 dropped=1"
    ]


def test_extracting_again_replaces_the_order(org: Org_) -> None:
    visit_id = transcribed(org)
    run(visit_id, ALL_PLACED, answer(order_items=[item("Crunchy"), item("Zingo")]))

    with session_scope() as session:
        reprocess.reprocess(session, org.org_id, visit_id, "extract")

    run(visit_id, answer(order_items=[item("Rival", 1)]))

    assert order_of(visit_id) == [(0, "Rival", Decimal(1), "dabbe", None)]


def test_a_visit_with_no_order_has_none(org: Org_) -> None:
    visit_id = transcribed(org)

    run(visit_id, ALL_PLACED, answer())

    assert order_of(visit_id) == []
