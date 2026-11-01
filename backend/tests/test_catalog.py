"""catalog.py: importing an org pack into an org, and the keywords transcription gets (batch 8)."""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import delete, select, update

from countertalk import catalog, limits
from countertalk.db import session_scope
from countertalk.errors import Conflict
from countertalk.models import CatalogAlias, CatalogItem, Distributor, Org, Team
from countertalk.playbooks import OrgPack, load_org_pack
from tests.conftest import ORG_PACK_FIXTURES, edit_file


def new_org(name: str) -> str:
    with session_scope() as session:
        org = Org(name=name)
        session.add(org)
        session.flush()
        return org.id


def run_import(org_id: str, pack: OrgPack) -> catalog.Imported:
    with session_scope() as session:
        return catalog.import_org_pack(session, org_id, pack)


def keywords(org_id: str) -> list[str]:
    with session_scope(read_only=True) as session:
        return catalog.keywords_for(session, org_id)


def contents(org_id: str) -> dict[str, list[tuple[str, ...]]]:
    with session_scope(read_only=True) as session:

        def rows(*columns: object) -> list[tuple[str, ...]]:
            table = columns[0].class_  # type: ignore[attr-defined]
            query = select(*columns).where(table.org_id == org_id).order_by(*columns)  # type: ignore[arg-type]
            return [tuple(row) for row in session.execute(query)]

        return {
            "teams": rows(Team.key, Team.name),
            "distributors": rows(Distributor.key, Distributor.area),
            "items": rows(CatalogItem.key, CatalogItem.kind, CatalogItem.brand),
            "aliases": rows(CatalogAlias.text, CatalogAlias.source),
        }


def tiny() -> OrgPack:
    return load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)


def test_an_import_puts_the_org_on_the_pack_and_adds_its_rows() -> None:
    org_id = new_org("Tiny Org")

    imported = run_import(org_id, tiny())

    assert imported == catalog.Imported(teams=2, distributors=2, items=3, aliases=6)
    with session_scope(read_only=True) as session:
        org = session.get(Org, org_id)
        assert org is not None and (org.org_pack_id, org.org_pack_version) == ("tiny-demo", 1)
    assert contents(org_id) == {
        "teams": [("field", "Field team"), ("money", "Money desk")],
        "distributors": [("t1", "North"), ("t2", "South")],
        "items": [
            ("crunchy", "own", "Crunchy"),
            ("crunchy_salt", "own", "Crunchy"),
            ("rival", "competitor", "Rival"),
        ],
        "aliases": [
            ("crunchy", "pack"),
            ("crunchy salted", "pack"),
            ("rival", "pack"),
            ("क्रंची", "pack"),
            ("क्रंची सॉल्टेड", "pack"),
            ("राइवल", "pack"),
        ],
    }


def test_importing_again_adds_nothing_and_keeps_the_screens_edits() -> None:
    org_id = new_org("Tiny Org")
    run_import(org_id, tiny())
    with session_scope() as session:
        session.execute(
            update(CatalogItem).where(CatalogItem.key == "rival").values(brand="Rival Gold")
        )
        session.execute(delete(CatalogAlias).where(CatalogAlias.text == "राइवल"))
    before = contents(org_id)

    again = run_import(org_id, tiny())

    assert again == catalog.Imported(teams=0, distributors=0, items=0, aliases=0)
    assert contents(org_id) == before


def test_two_orgs_on_two_packs_each_get_their_own(pack_roots: tuple[Path, Path]) -> None:
    packs, books = pack_roots
    other = packs / "other-demo"
    (other / "v1").mkdir(parents=True)
    for name in ("org_pack.yaml", "skus.csv", "competitors.csv", "schemes.csv", "distributors.csv"):
        (other / "v1" / name).write_bytes((packs / "tiny-demo/v1" / name).read_bytes())
    edit_file(other / "v1/org_pack.yaml", "id: tiny-demo", "id: other-demo")
    edit_file(
        other / "v1/org_pack.yaml",
        "  field:\n    name: Field team",
        "  field:\n    name: Route team",
    )
    edit_file(other / "v1/skus.csv", "Tiny Snacks,Crunchy,,", "Tiny Snacks,Munchy,,")
    edit_file(other / "v1/distributors.csv", "t2,South Traders (example),South\n", "")
    first, second = new_org("First Org"), new_org("Second Org")

    run_import(first, load_org_pack("tiny-demo", 1, packs, books))
    run_import(second, load_org_pack("other-demo", 1, packs, books))

    assert contents(first)["teams"] == [("field", "Field team"), ("money", "Money desk")]
    assert contents(second)["teams"] == [("field", "Route team"), ("money", "Money desk")]
    assert [key for key, _ in contents(second)["distributors"]] == ["t1"]
    assert keywords(first) == ["Crunchy", "Rival", "Crunchy Salted"]
    assert keywords(second) == ["Munchy", "Crunchy", "Rival", "Crunchy Salted"]


def test_keywords_are_brands_first_then_products_active_only_and_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id = new_org("Tiny Org")
    run_import(org_id, tiny())
    assert keywords(org_id) == ["Crunchy", "Rival", "Crunchy Salted"]
    with session_scope() as session:
        session.execute(update(CatalogItem).where(CatalogItem.key == "rival").values(active=False))
    monkeypatch.setattr(limits, "STT_KEYWORDS_MAX", 1)

    assert keywords(org_id) == ["Crunchy"]


def test_a_product_named_with_its_brand_isnt_given_the_brand_twice(
    pack_roots: tuple[Path, Path],
) -> None:
    packs, books = pack_roots
    edit_file(packs / "tiny-demo/v1/skus.csv", "Crunchy,Salted,", "Crunchy,Crunchy Salted,")
    org_id = new_org("Tiny Org")

    run_import(org_id, load_org_pack("tiny-demo", 1, packs, books))

    assert keywords(org_id) == ["Crunchy", "Rival", "Crunchy Salted"]


def test_an_org_on_one_pack_refuses_another(pack_roots: tuple[Path, Path]) -> None:
    packs, books = pack_roots
    shutil.copytree(packs / "tiny-demo", packs / "other-demo")
    edit_file(packs / "other-demo/v1/org_pack.yaml", "id: tiny-demo", "id: other-demo")
    org_id = new_org("Tiny Org")
    run_import(org_id, tiny())

    with pytest.raises(Conflict) as error:
        run_import(org_id, load_org_pack("other-demo", 1, packs, books))

    assert error.value.code == "org_on_another_pack"
    with session_scope(read_only=True) as session:
        org = session.get(Org, org_id)
        assert org is not None and org.org_pack_id == "tiny-demo"
