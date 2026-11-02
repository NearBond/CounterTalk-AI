"""The catalog screen (batch 16b): items, aliases and the review queue, every change in the Edit
log. On the tiny pack: Crunchy (own brand) and Crunchy Salted (its product), Rival (competitor).
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from countertalk import catalog, catalog_admin, entities
from countertalk.db import constraint_name, session_scope
from countertalk.errors import Conflict, Invalid
from countertalk.models import (
    AliasReview,
    AliasReviewStatus,
    CatalogAlias,
    CatalogItem,
    CatalogKind,
    Edit,
    EditObject,
    Role,
)
from tests.conftest import a_user, sign_in
from tests.test_roles import tiny, tiny_org
from tests.test_visits import Org_, new_visit


@pytest.fixture
def org() -> Org_:
    return tiny_org()


@pytest.fixture
def admin(org: Org_) -> str:
    return a_user(org.org_id, Role.ADMIN, "The Admin")


def item_id(org: Org_, brand: str, product: str | None = None) -> str:
    with session_scope(read_only=True) as session:
        query = select(CatalogItem.id).where(
            CatalogItem.org_id == org.org_id, CatalogItem.brand == brand
        )
        query = query.where(
            CatalogItem.product.is_(None) if product is None else CatalogItem.product == product
        )
        return session.scalars(query).one()


def version_of(found: str) -> int:
    with session_scope(read_only=True) as session:
        return session.get_one(CatalogItem, found).version


def alias(org: Org_, admin: str, item: str, text: str) -> CatalogAlias:
    with session_scope() as session:
        found = catalog_admin.add_alias(session, org.org_id, admin, item, text)
        session.expunge(found)
        return found


def edits(org: Org_) -> list[tuple[str, str, Any, Any]]:
    with session_scope(read_only=True) as session:
        found = session.scalars(
            select(Edit)
            .where(Edit.org_id == org.org_id, Edit.visit_id.is_(None))
            .order_by(Edit.created_at)
        )
        return [(e.object, e.field, e.before, e.after) for e in found]


def linked(org: Org_, text: str) -> str | None:
    with session_scope(read_only=True) as session:
        return entities.match(text, entities.load_catalog(session, org.org_id).names).target


# --- items -----------------------------------------------------------------------------------


def test_two_admins_editing_one_item_the_second_is_refused(org: Org_, admin: str) -> None:
    rival = item_id(org, "Rival")
    other = a_user(org.org_id, Role.ADMIN, "Other Admin")
    with session_scope() as session:
        catalog_admin.change_item(session, org.org_id, admin, rival, 1, company="Rival Foods Ltd")

    with pytest.raises(Conflict) as refused, session_scope() as session:
        catalog_admin.change_item(session, org.org_id, other, rival, 1, company="Overwritten")

    assert refused.value.code == "item_changed"
    with session_scope(read_only=True) as session:
        found = session.get_one(CatalogItem, rival)
        assert (found.company, found.version) == ("Rival Foods Ltd", 2)


def test_an_item_added_and_changed_is_logged_field_by_field(org: Org_, admin: str) -> None:
    with session_scope() as session:
        found = catalog_admin.create_item(
            session, org.org_id, admin, CatalogKind.COMPETITOR, "Zorb Co", "Zorb", None
        )
        added = found.id
    with session_scope() as session:
        catalog_admin.change_item(session, org.org_id, admin, added, 1, brand="Zorbo", active=False)

    assert edits(org) == [
        (
            EditObject.CATALOG_ITEM,
            "created",
            None,
            {"kind": "competitor", "company": "Zorb Co", "brand": "Zorb", "product": None},
        ),
        (EditObject.CATALOG_ITEM, "brand", "Zorb", "Zorbo"),
        (EditObject.CATALOG_ITEM, "active", True, False),
    ]


@pytest.mark.parametrize(
    ("kind", "brand", "product", "code"),
    [
        (CatalogKind.OWN, "Crunchy", "Salted", "item_exists"),
        (CatalogKind.OWN, "Rival", None, "name_in_use"),  # Rival, the competitor, is said so
        (CatalogKind.COMPETITOR, "Crunchy", None, "name_in_use"),
    ],
)
def test_an_item_the_catalog_has_or_is_matched_on_is_refused(
    org: Org_, admin: str, kind: str, brand: str, product: str | None, code: str
) -> None:
    with pytest.raises(Conflict) as refused, session_scope() as session:
        catalog_admin.create_item(session, org.org_id, admin, kind, "Any Co", brand, product)

    assert refused.value.code == code


def test_a_deactivated_item_is_never_linked_nor_a_keyword(org: Org_, admin: str) -> None:
    rival = item_id(org, "Rival")
    assert linked(org, "Rival") == rival

    with session_scope() as session:
        catalog_admin.change_item(session, org.org_id, admin, rival, 1, active=False)

    assert linked(org, "Rival") is None
    assert linked(org, "राइवल") is None
    with session_scope(read_only=True) as session:
        assert "Rival" not in catalog.keywords_for(session, org.org_id)


def test_a_new_alias_links_at_once(org: Org_, admin: str) -> None:
    rival = item_id(org, "Rival")

    alias(org, admin, rival, "Raival")

    assert linked(org, "raival") == rival


def test_reimporting_the_pack_keeps_every_edit(org: Org_, admin: str) -> None:
    rival = item_id(org, "Rival")
    with session_scope() as session:
        catalog_admin.change_item(
            session, org.org_id, admin, rival, 1, company="Rival Ltd", active=False
        )
        removed = session.scalars(
            select(CatalogAlias.id).where(
                CatalogAlias.item_id == rival, CatalogAlias.text == "rival"
            )
        ).one()
        catalog_admin.remove_alias(session, org.org_id, admin, removed)
        catalog_admin.create_item(
            session, org.org_id, admin, CatalogKind.COMPETITOR, "Zorb Co", "Zorb", None
        )

    with session_scope() as session:
        imported = catalog.import_org_pack(session, org.org_id, tiny())

    assert (imported.items, imported.aliases) == (0, 0)
    with session_scope(read_only=True) as session:
        found = session.get_one(CatalogItem, rival)
        assert (found.company, found.active) == ("Rival Ltd", False)
        texts = session.scalars(
            select(CatalogAlias.text).where(CatalogAlias.item_id == rival)
        ).all()
        assert texts == ["राइवल"]
        assert session.scalar(select(func.count()).where(CatalogItem.brand == "Zorb")) == 1


# --- aliases ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("brand", "product", "text", "code"),
    [
        ("Crunchy", None, "rival", "alias_in_use"),  # Rival's alias
        ("Crunchy", None, "RIVAL", "alias_in_use"),  # in any case
        ("Rival", None, "Crunchy Salted", "alias_in_use"),  # the product's own name
        ("Rival", None, " . ", "alias_empty"),
    ],
)
def test_an_alias_another_item_is_matched_on_or_that_s_empty_is_refused(
    org: Org_, admin: str, brand: str, product: str | None, text: str, code: str
) -> None:
    before = edits(org)

    with pytest.raises((Conflict, Invalid)) as refused:
        alias(org, admin, item_id(org, brand, product), text)

    assert refused.value.code == code
    assert edits(org) == before


def test_a_product_and_its_brand_may_share_a_name(org: Org_, admin: str) -> None:
    """Linking prefers the product of the two, so it isn't ambiguous."""
    salted = item_id(org, "Crunchy", "Salted")

    added = alias(org, admin, salted, "Crunchy")

    assert added.item_id == salted


def test_the_same_alias_again_is_the_one_there(org: Org_, admin: str) -> None:
    rival = item_id(org, "Rival")
    first = alias(org, admin, rival, "Raival")

    again = alias(org, admin, rival, "Raival")

    assert again.id == first.id
    assert [e[1] for e in edits(org)] == ["created"]


def test_an_alias_removed_is_logged_with_what_it_was(org: Org_, admin: str) -> None:
    added = alias(org, admin, item_id(org, "Rival"), "Raival")

    with session_scope() as session:
        catalog_admin.remove_alias(session, org.org_id, admin, added.id)

    assert edits(org)[-1] == (
        EditObject.CATALOG_ALIAS,
        "deleted",
        {"item_id": added.item_id, "text": "Raival", "source": "admin"},
        None,
    )


def test_a_brand_alias_that_makes_a_product_s_name_another_item_s_is_refused(
    org: Org_, admin: str
) -> None:
    """Linking reads a product as each of its brand's names and its own: "Krunch Salted"."""
    alias(org, admin, item_id(org, "Rival"), "Krunch Salted")

    with pytest.raises(Conflict) as refused:
        alias(org, admin, item_id(org, "Crunchy"), "Krunch")

    assert (refused.value.code, refused.value.details) == (
        "alias_in_use",
        {"item_id": item_id(org, "Rival")},
    )


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"company": None}, "name_needed"),
        ({"brand": " "}, "name_needed"),
        ({"active": None}, "active_needed"),
    ],
)
def test_a_change_to_nothing_is_refused(
    org: Org_, admin: str, changes: dict[str, Any], code: str
) -> None:
    with pytest.raises(Invalid) as refused, session_scope() as session:
        catalog_admin.change_item(session, org.org_id, admin, item_id(org, "Rival"), 1, **changes)

    assert refused.value.code == code
    assert version_of(item_id(org, "Rival")) == 1


# --- the review queue ------------------------------------------------------------------------


def reviews_of(org: Org_, text: str, visits: int) -> list[str]:
    """A name linking couldn't place, said in that many visits: its reviews' IDs."""
    ids = []
    with session_scope() as session:
        for _ in range(visits):
            review = AliasReview(
                org_id=org.org_id,
                kind=CatalogKind.COMPETITOR,
                text=text,
                normalised=entities.link_key(text),
                visit_id=new_visit(org),
            )
            session.add(review)
            session.flush()
            ids.append(review.id)
    return ids


def statuses(ids: list[str]) -> list[str]:
    with session_scope(read_only=True) as session:
        return [session.get_one(AliasReview, i).status for i in ids]


def resolve(org: Org_, admin: str, review: str, action: str, **more: Any) -> str | None:
    with session_scope() as session:
        found = catalog_admin.resolve(session, org.org_id, admin, review, action, **more)
        return None if found is None else found.id


def test_the_queue_lists_a_name_once_with_its_visits(org: Org_) -> None:
    reviews_of(org, "Zorblax", 2)
    reviews_of(org, "Kwik", 1)

    with session_scope(read_only=True) as session:
        found = catalog_admin.open_reviews(session, org.org_id)

    assert [(r.text, len(r.visit_ids)) for r in found] == [("Zorblax", 2), ("Kwik", 1)]


def test_attaching_a_name_makes_it_an_alias_and_resolves_every_visit_s(
    org: Org_, admin: str
) -> None:
    ids = reviews_of(org, "Raival", 2)
    rival = item_id(org, "Rival")

    target = resolve(org, admin, ids[0], "attach", item_id=rival)

    assert target == rival
    assert statuses(ids) == [AliasReviewStatus.ATTACHED] * 2
    assert linked(org, "raival") == rival


def test_creating_an_item_from_a_name(org: Org_, admin: str) -> None:
    [review] = reviews_of(org, "Zorblax", 1)

    made = resolve(
        org, admin, review, "create", new_item={"company": "Zorb Co", "brand": "Zorblax"}
    )

    assert made is not None and linked(org, "zorblax") == made
    with session_scope(read_only=True) as session:
        assert session.get_one(CatalogItem, made).kind == CatalogKind.COMPETITOR


def test_resolving_twice_changes_nothing_and_another_way_is_refused(org: Org_, admin: str) -> None:
    [review] = reviews_of(org, "Zorblax", 1)
    resolve(org, admin, review, "dismiss")
    logged = edits(org)

    again = resolve(org, admin, review, "dismiss")

    assert again is None
    assert edits(org) == logged
    with pytest.raises(Conflict) as refused:
        resolve(org, admin, review, "attach", item_id=item_id(org, "Rival"))
    assert refused.value.code == "review_resolved"
    assert statuses([review]) == [AliasReviewStatus.DISMISSED]


def test_a_name_said_as_a_competitor_isn_t_attached_to_an_own_item(org: Org_, admin: str) -> None:
    [review] = reviews_of(org, "Krunchi", 1)

    with pytest.raises(Invalid) as refused:
        resolve(org, admin, review, "attach", item_id=item_id(org, "Crunchy"))

    assert refused.value.code == "kind_differs"
    assert statuses([review]) == [AliasReviewStatus.OPEN]


# --- the Edit log's rows with no visit -------------------------------------------------------


@pytest.mark.parametrize(
    ("what", "with_visit"),
    [(EditObject.INSIGHT, False), (EditObject.CATALOG_ITEM, True)],
)
def test_a_visit_s_edit_has_a_visit_and_the_catalog_s_none(
    org: Org_, admin: str, what: str, with_visit: bool
) -> None:
    visit_id = new_visit(org) if with_visit else None

    with pytest.raises(IntegrityError) as refused, session_scope() as session:
        session.add(
            Edit(
                org_id=org.org_id,
                visit_id=visit_id,
                actor_id=admin,
                object=what,
                object_id=org.shop_id,
                field="created",
                before=None,
                after={"x": 1},
            )
        )

    assert constraint_name(refused.value) == "ck_edits_visit_or_catalog"


# --- the API ---------------------------------------------------------------------------------


def test_the_api_adds_edits_and_names_an_item(client: TestClient, org: Org_, admin: str) -> None:
    sign_in(client, admin)

    made = client.post(
        "/api/catalog/items", json={"kind": "competitor", "company": "Zorb Co", "brand": "Zorb"}
    )
    url = f"/api/catalog/items/{made.json()['id']}"
    named = client.post(f"{url}/aliases", json={"text": "ज़ोर्ब"})
    stale = client.patch(url, json={"version": 0, "active": False})
    changed = client.patch(url, json={"version": 1, "product": "Waves"})
    found = client.get("/api/catalog/items", params={"q": "ज़ोर्ब"}).json()["items"]

    assert (made.status_code, named.status_code, stale.status_code) == (201, 201, 409)
    assert [a["text"] for a in named.json()["aliases"]] == ["ज़ोर्ब"]
    assert (changed.json()["product"], changed.json()["version"]) == ("Waves", 2)
    assert [(i["brand"], i["product"]) for i in found] == [("Zorb", "Waves")]
    alias_id = named.json()["aliases"][0]["id"]
    assert client.delete(f"/api/catalog/aliases/{alias_id}").status_code == 204


def test_the_api_s_review_queue_resolves_a_name(client: TestClient, org: Org_, admin: str) -> None:
    reviews_of(org, "Raival", 2)
    sign_in(client, admin)

    [shown] = client.get("/api/catalog/reviews").json()
    body = {"action": "attach", "item_id": item_id(org, "Rival")}
    resolved = client.post(f"/api/catalog/reviews/{shown['id']}/resolve", json=body)

    assert (shown["text"], len(shown["visit_ids"])) == ("Raival", 2)
    assert resolved.status_code == 200
    assert "Raival" in [a["text"] for a in resolved.json()["item"]["aliases"]]
    assert client.get("/api/catalog/reviews").json() == []


def test_creating_needs_the_item_and_attaching_an_active_one(org: Org_, admin: str) -> None:
    [review] = reviews_of(org, "Zorblax", 1)
    rival = item_id(org, "Rival")
    with session_scope() as session:
        catalog_admin.change_item(session, org.org_id, admin, rival, 1, active=False)

    with pytest.raises(Invalid) as no_item:
        resolve(org, admin, review, "create")
    with pytest.raises(Conflict) as inactive:
        resolve(org, admin, review, "attach", item_id=rival)

    assert (no_item.value.code, inactive.value.code) == ("item_needed", "item_inactive")
    assert statuses([review]) == [AliasReviewStatus.OPEN]


def test_attaching_again_to_another_item_is_refused(org: Org_, admin: str) -> None:
    [review] = reviews_of(org, "Raival", 1)
    rival = item_id(org, "Rival")
    resolve(org, admin, review, "attach", item_id=rival)
    with session_scope() as session:
        other = catalog_admin.create_item(
            session, org.org_id, admin, CatalogKind.COMPETITOR, "Zorb Co", "Zorb"
        ).id

    assert resolve(org, admin, review, "attach", item_id=rival) is None  # the same: nothing changes
    with pytest.raises(Conflict) as refused:
        resolve(org, admin, review, "attach", item_id=other)
    assert refused.value.code == "review_resolved"


def test_an_item_made_from_its_own_name_has_no_alias_of_it(org: Org_, admin: str) -> None:
    [review] = reviews_of(org, "Zorblax", 1)

    made = resolve(
        org, admin, review, "create", new_item={"company": "Zorb Co", "brand": "Zorblax"}
    )

    with session_scope(read_only=True) as session:
        assert (
            session.scalars(select(CatalogAlias.text).where(CatalogAlias.item_id == made)).all()
            == []
        )
    assert linked(org, "Zorblax") == made
