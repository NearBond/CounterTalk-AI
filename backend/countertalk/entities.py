"""Linking (batch 12; BRIEF §7): the names an insight mentions, matched to its org's own catalog,
and its scheme key to the org pack's schemes.

The key a name is matched on is verify's normalise() (R1: one normaliser) with its spaces
removed ("Lay's" and "lays" are both "lays"), and two folds for the commonest ways speech-to-text
and the model spell a Hindi word differently: the long vowel signs ी and ू as the short ones,
and a nasal consonant with a virama before a consonant as an anusvara (कैम्पा is कैंपा). The
folds are linking's alone; quote matching is unchanged.

A catalog item is matched on its aliases; a brand also on its brand name; a product also on
"brand product", and on each of its brand's names followed by each of its own in the same
script, so "बिंगो मैड एंगल्स" is exact. Within each script: no transliteration (batch 12's
note), since every item has Devanagari aliases.

The order: an exact key; otherwise a fuzzy one (rapidfuzz ratio, LINK_FUZZY_MIN_SCORE or more,
between keys of LINK_FUZZY_MIN_CHARS or more); otherwise unknown. Any other item within
LINK_NEAR_POINTS of the best makes the name ambiguous, but for a product and its own brand: the
higher wins, the product on a tie. Unknown and ambiguous names go to AliasReview, never to the
nearest guess (§9.6).

An insight holds one link of each kind: the first mention that resolves to one. The catalog
decides the kind, whichever list the model put the name in (D7: code decides), but a name in its
own list comes first.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from countertalk import limits
from countertalk.models import AliasReview, CatalogAlias, CatalogItem, CatalogKind
from countertalk.playbooks import Scheme
from countertalk.verify import normalise

# Written with chr(), so the source shows which code points they are. ी to ि, ू to ु:
_LONG_TO_SHORT = str.maketrans({chr(0x0940): chr(0x093F), chr(0x0942): chr(0x0941)})
# ङ ञ ण न म, then a virama, before a consonant (क to ह):
_NASALS = "".join(chr(c) for c in (0x0919, 0x091E, 0x0923, 0x0928, 0x092E))
_NASAL_CLUSTER = re.compile(f"[{_NASALS}]{chr(0x094D)}(?=[{chr(0x0915)}-{chr(0x0939)}])")
_ANUSVARA = chr(0x0902)
_DEVANAGARI = re.compile(f"[{chr(0x0900)}-{chr(0x097F)}]")

How = Literal["exact", "fuzzy", "unknown", "ambiguous"]


def link_key(text: str) -> str:
    key = normalise(text).text.replace(" ", "").translate(_LONG_TO_SHORT)
    return _NASAL_CLUSTER.sub(_ANUSVARA, key)


@dataclass(frozen=True)
class Names:
    """What a set of targets (catalog items, or schemes) is matched on."""

    by_key: dict[str, frozenset[str]]
    # A product's brand item.
    brand_of: dict[str, str]
    targets: frozenset[str]


@dataclass(frozen=True)
class Found:
    target: str | None
    how: How
    score: float
    # The name's linking key.
    key: str


def names_of(named: Iterable[tuple[str, Iterable[str]]], brand_of: dict[str, str]) -> Names:
    by_key: dict[str, set[str]] = defaultdict(set)
    targets = set()
    for target, texts in named:
        targets.add(target)
        for text in texts:
            if key := link_key(text):
                by_key[key].add(target)
    found = {key: frozenset(them) for key, them in by_key.items()}
    return Names(found, brand_of, frozenset(targets))


def match(text: str, names: Names) -> Found:
    wanted = link_key(text)
    exact = names.by_key.get(wanted)
    if exact:
        return _pick(dict.fromkeys(exact, 100.0), names, "exact", wanted)
    # Below LINK_FUZZY_MIN_CHARS, one letter more or less still scores 85 (Coker and coke: 89).
    scores: dict[str, float] = {}
    if len(wanted) >= limits.LINK_FUZZY_MIN_CHARS:
        for key, targets in names.by_key.items():
            if len(key) >= limits.LINK_FUZZY_MIN_CHARS:
                score = fuzz.ratio(wanted, key)
                for target in targets:
                    scores[target] = max(score, scores.get(target, 0.0))
    best = max(scores.values(), default=0.0)
    if best < limits.LINK_FUZZY_MIN_SCORE:
        return Found(None, "unknown", best, wanted)
    near = {t: score for t, score in scores.items() if score >= best - limits.LINK_NEAR_POINTS}
    return _pick(near, names, "fuzzy", wanted)


def _pick(scores: dict[str, float], names: Names, how: How, key: str) -> Found:
    """The one target left, or ambiguous. A product and its own brand aren't rivals: the
    higher stays, the product on a tie (either is the right brand)."""
    left = set(scores)
    for product, brand in names.brand_of.items():
        if product in scores and brand in scores:
            left.discard(brand if scores[product] >= scores[brand] else product)
    if len(left) == 1:
        target = left.pop()
        return Found(target, how, scores[target], key)
    return Found(None, "ambiguous", max(scores.values()), key)


# --- the catalog -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str
    company: str
    brand: str
    # None for a brand as a whole.
    product: str | None
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Catalog:
    names: Names
    kinds: dict[str, str]


def catalog_of(entries: Sequence[Entry]) -> Catalog:
    brands = {(e.company, e.brand): e for e in entries if e.product is None}
    named, brand_of = [], {}
    for entry in entries:
        if entry.product is None:
            named.append((entry.id, [entry.brand, *entry.aliases]))
            continue
        brand = brands.get((entry.company, entry.brand))
        brand_names = [entry.brand, *(brand.aliases if brand else ())]
        own = [entry.product, *entry.aliases]
        combined = [f"{b} {p}" for b in brand_names for p in own if _script(b) == _script(p)]
        named.append((entry.id, [*entry.aliases, *combined]))
        if brand is not None:
            brand_of[entry.id] = brand.id
    return Catalog(names_of(named, brand_of), {e.id: e.kind for e in entries})


def _script(text: str) -> bool:
    return _DEVANAGARI.search(text) is not None


def load_catalog(session: Session, org_id: str) -> Catalog:
    """The org's own active items: another org's never link, nor a deactivated one."""
    active = (CatalogItem.org_id == org_id, CatalogItem.active.is_(True))
    aliases: dict[str, list[str]] = defaultdict(list)
    query = select(CatalogAlias.item_id, CatalogAlias.text).join(
        CatalogItem, CatalogItem.id == CatalogAlias.item_id
    )
    for item_id, text in session.execute(query.where(*active)):
        aliases[item_id].append(text)
    items = session.execute(
        select(CatalogItem.id, CatalogItem.kind, CatalogItem.company)
        .add_columns(CatalogItem.brand, CatalogItem.product)
        .where(*active)
    )
    return catalog_of(
        [
            Entry(
                id=row.id,
                kind=row.kind,
                company=row.company,
                brand=row.brand,
                product=row.product,
                aliases=tuple(aliases[row.id]),
            )
            for row in items
        ]
    )


# --- an insight's links ----------------------------------------------------------------------


@dataclass(frozen=True)
class Review:
    # The kind of the list the name came from.
    kind: str
    text: str
    normalised: str


@dataclass(frozen=True)
class Links:
    sku_id: str | None
    competitor_id: str | None
    reviews: tuple[Review, ...]


def link_mentions(
    catalog: Catalog,
    sku_mentions: Sequence[str],
    competitor_mentions: Sequence[str],
    tally: Counter,
) -> Links:
    """Counts each mention's outcome in tally, and wrong_kind for one filed in the other list."""
    resolved: dict[str, list[str]] = {CatalogKind.OWN: [], CatalogKind.COMPETITOR: []}
    elsewhere: dict[str, list[str]] = {CatalogKind.OWN: [], CatalogKind.COMPETITOR: []}
    reviews: list[Review] = []
    for listed, mentions in (
        (CatalogKind.OWN, sku_mentions),
        (CatalogKind.COMPETITOR, competitor_mentions),
    ):
        for text in mentions:
            found = match(text, catalog.names)
            tally[found.how] += 1
            if found.target is None:
                if found.key:
                    reviews.append(Review(listed, text, found.key))
                continue
            kind = catalog.kinds[found.target]
            if kind != listed:
                tally["wrong_kind"] += 1
            (resolved if kind == listed else elsewhere)[kind].append(found.target)

    def first(kind: str) -> str | None:
        return next(iter(resolved[kind] + elsewhere[kind]), None)

    return Links(first(CatalogKind.OWN), first(CatalogKind.COMPETITOR), tuple(reviews))


def add_reviews(session: Session, org_id: str, visit_id: str, reviews: Iterable[Review]) -> int:
    """Once per name and kind per visit, so a rerun adds nothing (§2.5). How many were new."""
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for r in reviews:  # the first way it was written is kept
        rows.setdefault(
            (r.kind, r.normalised),
            {
                "org_id": org_id,
                "visit_id": visit_id,
                "kind": r.kind,
                "text": r.text,
                "normalised": r.normalised,
            },
        )
    if not rows:
        return 0
    table = AliasReview.__table__
    query = (
        insert(table)
        .values(list(rows.values()))
        .on_conflict_do_nothing(index_elements=["org_id", "visit_id", "kind", "normalised"])
        .returning(table.c.id)
    )
    return len(session.execute(query).all())


# --- schemes ---------------------------------------------------------------------------------


def scheme_names(schemes: Sequence[Scheme]) -> Names:
    return names_of(((s.key, [s.key, s.name, *s.aliases]) for s in schemes), {})


def link_scheme(names: Names, scheme_key: str | None, tally: Counter) -> str | None:
    """The pack's own key, or None: one not in the pack goes through the same matching, and
    if it matches no scheme, or two, it's dropped and counted (never guessed)."""
    if scheme_key is None or scheme_key in names.targets:
        return scheme_key
    found = match(scheme_key, names)
    if found.target is None:
        tally["scheme_dropped"] += 1
    elif found.target != scheme_key:
        tally["scheme_by_alias"] += 1
    return found.target
