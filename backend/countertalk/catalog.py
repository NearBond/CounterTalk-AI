"""An org's catalog, teams and distributors in the database, seeded from its org pack (batch 8).

The import only adds what's missing, keyed by (org_id, key), and never changes or deletes a row:
edits made later on the catalog screen (batch 16b) survive a re-import. An item's aliases come in
with the item, so an alias removed on the screen stays removed. Teams' routing and what they
handle stay in the pack's files; schemes too.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import limits
from countertalk.errors import Conflict
from countertalk.models import (
    AliasSource,
    CatalogAlias,
    CatalogItem,
    CatalogKind,
    Distributor,
    Org,
    Team,
)
from countertalk.playbooks import CatalogRow, OrgPack

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Imported:
    """What an import added: all zeros the second time."""

    teams: int
    distributors: int
    items: int
    aliases: int


def _insert_missing(
    session: Session, model: type[Any], rows: list[dict[str, Any]]
) -> dict[str, str]:
    """Inserts the rows whose (org_id, key) is free; returns the new rows' {key: id}."""
    if not rows:
        return {}
    table = model.__table__
    inserted = session.execute(
        insert(table)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["org_id", "key"])
        .returning(table.c.key, table.c.id)
    )
    return {key: item_id for key, item_id in inserted}


def import_org_pack(session: Session, org_id: str, pack: OrgPack) -> Imported:
    """Puts the org on this pack and adds its missing teams, distributors and catalog items."""
    org = session.scalars(
        select(Org)
        .where(Org.id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if org.org_pack_id not in (None, pack.id):
        # Its teams, distributors and catalog came from that pack: mixing two isn't an import.
        raise Conflict(
            "org_on_another_pack",
            f"This org is on the {org.org_pack_id} pack, so {pack.id} can't be imported into it",
        )
    org.org_pack_id, org.org_pack_version = pack.id, max(pack.version, org.org_pack_version or 0)
    teams = _insert_missing(
        session,
        Team,
        [
            {"org_id": org_id, "key": key, "name": team.name}
            for key, team in pack.file.teams.items()
        ],
    )
    distributors = _insert_missing(
        session,
        Distributor,
        [
            {"org_id": org_id, "key": d.key, "name": d.name, "area": d.area}
            for d in pack.distributors
        ],
    )
    rows: dict[str, tuple[CatalogRow, CatalogKind]] = {
        row.key: (row, CatalogKind.OWN) for row in pack.skus
    }
    rows |= {row.key: (row, CatalogKind.COMPETITOR) for row in pack.competitors}
    new_items = _insert_missing(
        session,
        CatalogItem,
        [
            {
                "org_id": org_id,
                "key": row.key,
                "kind": kind,
                "company": row.company,
                "brand": row.brand,
                "product": row.product,
            }
            for row, kind in rows.values()
        ],
    )
    aliases = [
        {"org_id": org_id, "item_id": item_id, "text": text, "source": AliasSource.PACK}
        for key, item_id in new_items.items()
        for text in rows[key][0].aliases
    ]
    added = 0
    if aliases:
        table = CatalogAlias.__table__
        added = len(session.execute(insert(table).values(aliases).returning(table.c.id)).all())
    imported = Imported(len(teams), len(distributors), len(new_items), added)
    log.info(
        "org pack imported org=%s pack=%s version=%d teams=%d distributors=%d items=%d aliases=%d",
        org_id,
        pack.id,
        pack.version,
        *vars(imported).values(),
    )
    return imported


def keywords_for(session: Session, org_id: str) -> list[str]:
    """What transcription is told to listen for (batch 8): the org's brands, its own first, then
    its products, at most STT_KEYWORDS_MAX. Active items only, in a fixed order.

    One over STT_KEYWORD_MAX_CHARS is left out, and the next takes its place: Meta refuses the
    whole request for it, and a cut name would be a wrong keyword."""
    items = session.execute(
        select(CatalogItem.kind, CatalogItem.brand, CatalogItem.product)
        .where(CatalogItem.org_id == org_id, CatalogItem.active.is_(True))
        .order_by(CatalogItem.kind != CatalogKind.OWN, CatalogItem.key)
    ).all()
    brands = [brand for _, brand, _ in items]
    products = [
        product if product.lower().startswith(brand.lower()) else f"{brand} {product}"
        for _, brand, product in items
        if product is not None
    ]
    candidates = list(dict.fromkeys(brands + products))
    # Characters as Python counts them, code points: the catalog's names are Latin today. A
    # non-ASCII name would need checking against how Meta counts its 30 (bytes, perhaps).
    fitting = [word for word in candidates if len(word) <= limits.STT_KEYWORD_MAX_CHARS]
    if len(fitting) < len(candidates):
        log.info(
            "catalog keywords too long for speech-to-text, left out org=%s count=%d",
            org_id,
            len(candidates) - len(fitting),
        )
    return fitting[: limits.STT_KEYWORDS_MAX]
