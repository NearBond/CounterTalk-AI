"""entities.py: names linked to the org's own catalog, or sent to review (batch 12)."""

import re
from collections import Counter
from datetime import date

import pytest
from sqlalchemy import select, update

from countertalk import catalog, entities
from countertalk.db import session_scope
from countertalk.entities import Entry, Review
from countertalk.models import AliasReview, CatalogItem, CatalogKind
from countertalk.playbooks import ORG_PACKS_DIR, PLAYBOOKS_DIR, OrgPack, Scheme, load_org_pack
from countertalk.verify import normalise
from tests.test_roles import tiny, tiny_org
from tests.test_visits import Org_, make_org, new_visit

OWN, THEIRS = CatalogKind.OWN, CatalogKind.COMPETITOR


def pepsico() -> OrgPack:
    return load_org_pack("pepsico-india-demo", 1, ORG_PACKS_DIR, PLAYBOOKS_DIR)


def catalog_from(pack: OrgPack) -> entities.Catalog:
    """The pack's catalog with each item's key as its ID, so a link reads as its key."""
    rows = [(row, OWN) for row in pack.skus] + [(row, THEIRS) for row in pack.competitors]
    return entities.catalog_of(
        [Entry(r.key, kind, r.company, r.brand, r.product, r.aliases) for r, kind in rows]
    )


PEPSICO = catalog_from(pepsico())

# (as the model wrote it, the item it links to or None, how). Brands in both scripts, spelt the
# way speech-to-text and the model spell them; products; names in another org's pack (the tests'
# tiny-demo) and in none.
TRICKY = [
    # K1's mentions (pass 3)
    ("Kurkure", "kurkure", "exact"),
    ("कुरकुरे", "kurkure", "exact"),
    ("लेज़", "lays", "exact"),
    ("Lay's", "lays", "exact"),
    ("लेस", "lays", "exact"),  # how speech-to-text spells it; at 3 letters, exact only
    ("बिंगो", "bingo", "exact"),
    ("Bingo", "bingo", "exact"),
    ("बालाजी", "balaji", "exact"),
    ("बालाजी वेफर्स", "balaji", "exact"),  # step 1's live run (13e)
    ("क्वेकर ओट्स", "quaker", "exact"),
    ("क्रीम ओनियन", "lays_cream_onion", "exact"),
    ("डोरीटोस", "doritos", "exact"),  # the long ी folded: the catalog writes डोरिटोस
    # spacing, punctuation and spelling
    ("Kur Kure", "kurkure", "exact"),
    ("Thumbs Up", "thums_up", "exact"),
    ("थम्सअप", "thums_up", "exact"),
    ("7 Up", "seven_up", "exact"),
    ("Kurkuray", "kurkure", "fuzzy"),
    ("Doritoz", "doritos", "fuzzy"),
    ("Haldiraam", "haldirams", "fuzzy"),
    ("Balaaji", "balaji", "fuzzy"),
    ("कैंपा", "campa", "exact"),  # the catalog writes कैम्पा: a nasal and virama fold to ं
    ("बिन्गो", "bingo", "exact"),
    ("कुरकुरें", "kurkure", "fuzzy"),  # nearer its brand (93) than its product कुरकुरे मंच (89)
    ("Sprites", "sprite", "fuzzy"),
    ("चीटोज़", "cheetos", "exact"),  # the spoken plural, a catalog alias
    # products, which beat their brands
    ("मैड एंगल्स", "bingo_mad_angles", "exact"),
    ("Mad Angles", "bingo_mad_angles", "exact"),
    ("Bingo Mad Angles", "bingo_mad_angles", "exact"),
    ("बिंगो मैड एंगल्स", "bingo_mad_angles", "exact"),
    ("Mad Angle", "bingo_mad_angles", "fuzzy"),
    ("मैड एंगल", "bingo_mad_angles", "fuzzy"),
    ("Kurkure Masala Munch", "kurkure_masala_munch", "exact"),
    ("कुरकुरे मसाला मंच", "kurkure_masala_munch", "exact"),
    ("Lays Magic Masala", "lays_magic_masala", "exact"),
    ("Campa Cola", "campa_cola", "exact"),
    ("Campa", "campa", "exact"),
    # too far from any name to be more than a guess
    ("Nimbuz", None, "unknown"),
    ("Masala", None, "unknown"),
    ("Kurkure Lays", None, "unknown"),
    # short: a letter more or less still scores 85 or more, so it isn't trusted
    ("Coker", None, "unknown"),  # coke
    ("Lima", None, "unknown"),  # limca
    ("Dewa", None, "unknown"),  # dew
    # only in another org's pack
    ("Crunchy", None, "unknown"),  # 80 against Balaji's "crunchem"
    ("क्रंची", None, "unknown"),
    ("Rival", None, "unknown"),
    ("राइवल", None, "unknown"),
    # in no pack
    ("Blinkit", None, "unknown"),
    ("ब्लिंकिट", None, "unknown"),
    ("Britannia", None, "unknown"),
]


@pytest.mark.parametrize(("text", "item", "how"), TRICKY, ids=[t for t, _, _ in TRICKY])
def test_tricky_names_link_right_or_go_to_review(text: str, item: str | None, how: str) -> None:
    found = entities.match(text, PEPSICO.names)

    assert (found.target, found.how) == (item, how)


@pytest.mark.parametrize(
    ("one", "other"),
    [("डोरीटोस", "डोरिटोस"), ("कूल", "कुल"), ("कैम्पा", "कैंपा"), ("हिन्दी", "हिंदी")],
)
def test_the_folds_are_linkings_alone(one: str, other: str) -> None:
    assert entities.link_key(one) == entities.link_key(other)
    # Quote matching keeps them apart.
    assert normalise(one).text != normalise(other).text


def test_a_nasal_before_a_vowel_is_not_folded() -> None:
    assert entities.link_key("नमन") != entities.link_key("नंन")


def test_a_brand_and_a_product_name_are_joined_only_in_one_script() -> None:
    devanagari = re.compile(f"[{chr(0x0900)}-{chr(0x097F)}]")
    keys = PEPSICO.names.by_key
    assert [k for k in keys if devanagari.search(k) and re.search("[a-z]", k)] == []


def entry(item_id: str, brand: str, product: str | None = None, *aliases: str) -> Entry:
    return Entry(item_id, OWN, "Co", brand, product, aliases)


def test_two_items_as_near_as_each_other_are_ambiguous() -> None:
    near = entities.catalog_of([entry("bite", "Tasty Bite"), entry("byte", "Tasty Byte")])

    found = entities.match("Tasty Bote", near.names)

    assert (found.target, found.how) == (None, "ambiguous")


def test_an_item_a_few_points_behind_the_best_makes_it_ambiguous() -> None:
    near = entities.catalog_of([entry("bar", "Crunch Bar"), entry("bars", "Crunchy Bar")])

    found = entities.match("Crunchh Bar", near.names)  # 94.7 and 90.0

    assert (found.target, found.how) == (None, "ambiguous")


def test_one_name_on_two_items_is_ambiguous_not_the_first() -> None:
    twice = entities.catalog_of(
        [entry("a", "Alpha", None, "masala"), entry("b", "Beta", None, "masala")]
    )

    assert entities.match("Masala", twice.names) == entities.Found(
        None, "ambiguous", 100.0, "masala"
    )


def test_a_product_beats_its_own_brand_but_not_a_sibling() -> None:
    # The pack gave the brand its product's alias too: the product still wins.
    family = entities.catalog_of(
        [
            entry("mad", "Mad", None, "mad angles", "zig zag"),
            entry("angles", "Mad", "Angles", "mad angles"),
            entry("zig", "Mad", "Zig", "zig zag"),
            entry("zag", "Mad", "Zag", "zig zag"),
        ]
    )

    assert entities.match("Mad Angles", family.names).target == "angles"
    assert entities.match("Zig Zag", family.names).how == "ambiguous"


def test_a_blank_name_matches_nothing() -> None:
    assert entities.match(" ?! ", PEPSICO.names) == entities.Found(None, "unknown", 0.0, "")


# --- an insight's links ----------------------------------------------------------------------


def link(sku: list[str], theirs: list[str]) -> tuple[entities.Links, Counter]:
    tally: Counter = Counter()
    return entities.link_mentions(PEPSICO, sku, theirs, tally), tally


def test_each_link_is_the_first_mention_that_resolves() -> None:
    links, tally = link(["Blinkit", "Kurkure", "Lays"], ["डोरीटोस", "Bingo", "Balaji"])

    assert (links.sku_id, links.competitor_id) == ("kurkure", "bingo")
    assert links.reviews == (Review(OWN, "Blinkit", "blinkit"),)
    # डोरीटोस is the org's own: filed with the competitors, it's counted.
    assert dict(tally) == {"exact": 5, "unknown": 1, "wrong_kind": 1}


def test_the_catalog_decides_the_kind_but_a_name_in_its_own_list_comes_first() -> None:
    misfiled, _ = link(["Bingo"], [])
    both, tally = link(["Bingo"], ["Balaji"])

    assert (misfiled.sku_id, misfiled.competitor_id) == (None, "bingo")
    assert (both.sku_id, both.competitor_id) == (None, "balaji")
    assert tally["wrong_kind"] == 1


def test_an_unknown_name_is_reviewed_as_the_kind_of_its_list() -> None:
    links, tally = link(["Kurkure"], ["Blinkit", "Crunchy", "?!"])

    assert links.competitor_id is None
    assert links.reviews == (
        Review(THEIRS, "Blinkit", "blinkit"),
        Review(THEIRS, "Crunchy", "crunchy"),
    )
    assert dict(tally) == {"exact": 1, "unknown": 3}  # the blank one isn't reviewed


# --- the org's own catalog, from the database ------------------------------------------------


def org_on(pack: OrgPack) -> Org_:
    org = make_org(pack.file.display_name)
    with session_scope() as session:
        catalog.import_org_pack(session, org.org_id, pack)
    return org


def linked(org: Org_, text: str) -> str | None:
    with session_scope(read_only=True) as session:
        found = entities.match(text, entities.load_catalog(session, org.org_id).names)
        item = session.get(CatalogItem, found.target) if found.target else None
        return None if item is None else item.key


def test_each_org_links_only_to_its_own_catalog() -> None:
    theirs, ours = tiny_org(), org_on(pepsico())

    assert [linked(theirs, t) for t in ("Kurkure", "क्रंची सॉल्टेड", "Rival")] == [
        None,
        "crunchy_salt",
        "rival",
    ]
    assert [linked(ours, t) for t in ("Kurkure", "क्रंची", "Rival")] == ["kurkure", None, None]


def test_a_deactivated_item_never_links() -> None:
    org = tiny_org()
    with session_scope() as session:
        session.execute(
            update(CatalogItem)
            .where(CatalogItem.org_id == org.org_id, CatalogItem.key == "rival")
            .values(active=False)
        )

    assert [linked(org, t) for t in ("Rival", "राइवल")] == [None, None]


def reviews_of(org_id: str) -> list[tuple[str, str, str, str]]:
    with session_scope(read_only=True) as session:
        query = select(AliasReview.visit_id, AliasReview.kind, AliasReview.text)
        query = query.add_columns(AliasReview.status).where(AliasReview.org_id == org_id)
        return sorted(tuple(row) for row in session.execute(query))


def add(org: Org_, visit_id: str, *reviews: Review) -> int:
    with session_scope() as session:
        return entities.add_reviews(session, org.org_id, visit_id, reviews)


def test_a_name_is_reviewed_once_per_kind_per_visit() -> None:
    org = tiny_org()
    first, second = new_visit(org), new_visit(org)
    blinkit = Review(THEIRS, "Blinkit", "blinkit")

    assert add(org, first, blinkit, Review(THEIRS, "blinkit!", "blinkit")) == 1
    assert add(org, first, blinkit, Review(OWN, "Blinkit", "blinkit")) == 1  # a rerun adds none
    assert add(org, second, blinkit) == 1
    assert add(org, second) == 0

    assert reviews_of(org.org_id) == sorted(
        [
            (first, "competitor", "Blinkit", "open"),
            (first, "own", "Blinkit", "open"),
            (second, "competitor", "Blinkit", "open"),
        ]
    )


# --- schemes ---------------------------------------------------------------------------------

SCHEMES = entities.scheme_names(tiny().schemes)


@pytest.mark.parametrize(
    ("given", "kept", "counted"),
    [
        ("crunchy_winter", "crunchy_winter", {}),
        ("विंटर स्कीम", "crunchy_winter", {"scheme_by_alias": 1}),
        ("Crunchy winter payout", "crunchy_winter", {"scheme_by_alias": 1}),
        ("lays_display_rack", None, {"scheme_dropped": 1}),  # another org's scheme
        (None, None, {}),
    ],
)
def test_a_scheme_key_not_in_the_pack_is_matched_or_dropped(
    given: str | None, kept: str | None, counted: dict[str, int]
) -> None:
    tally: Counter = Counter()

    assert entities.link_scheme(SCHEMES, given, tally) == kept
    assert dict(tally) == counted


def test_a_key_in_the_pack_stays_even_when_another_schemes_alias_reads_the_same() -> None:
    day = date(2026, 11, 1)
    clashing = entities.scheme_names(
        [
            Scheme("diwali_scheme", "Diwali", "payout", ("crunchy",), day, day, ()),
            Scheme("festive", "Festive", "payout", ("crunchy",), day, day, ("Diwali scheme",)),
        ]
    )
    tally: Counter = Counter()

    assert entities.link_scheme(clashing, "diwali_scheme", tally) == "diwali_scheme"
    assert entities.link_scheme(clashing, "Diwali Scheme", tally) is None  # a name, not a key
    assert dict(tally) == {"scheme_dropped": 1}


# --- Marathi's case endings (batch T3) --------------------------------------------------------

MARATHI_ENDINGS = entities.endings_for(("mr", "hi", "en"))


@pytest.mark.parametrize(
    ("text", "item"),
    [
        ("लेज़चा", "lays"),  # three letters: exact or nothing, so only without its ending
        ("लेज़मध्ये", "lays"),
        ("बिंगोचं", "bingo"),
        ("कैम्पाची", "campa"),
        ("कुरकुरेमध्ये", "kurkure"),
        ("बिंगोवर", "bingo"),
        ("लेज़त", "lays"),
    ],
)
def test_a_marathi_ending_is_dropped_from_a_name_that_matched_nothing(text: str, item: str) -> None:
    assert entities.match(text, PEPSICO.names).how == "unknown"
    found = entities.match(text, PEPSICO.names, MARATHI_ENDINGS)

    assert (found.target, found.how) == (item, "exact")


def test_endings_are_marathi_s_alone_and_only_a_second_try() -> None:
    assert entities.endings_for(("hi", "en")) == ()
    # A name matched as it is stays so: कैम्पा कोला ends in ला, and its stem is only near it.
    found = entities.match("कैम्पा कोला", PEPSICO.names, MARATHI_ENDINGS)
    assert (found.target, found.how) == ("campa_cola", "exact")
    # A stem that's only near a name, or nothing, leaves the whole name for review.
    near = entities.match("कुरकुरीचा", PEPSICO.names, MARATHI_ENDINGS)
    assert (near.target, near.how) == (None, "unknown")
    unknown = entities.match("ब्लिंकिटचा", PEPSICO.names, MARATHI_ENDINGS)
    assert (unknown.target, unknown.how, unknown.key) == (None, "unknown", "ब्लिंकिटचा")


def test_an_ambiguous_name_is_tried_without_its_ending_too() -> None:
    # Within LINK_NEAR_POINTS of both items with its ending; its stem is exactly one's name.
    names = entities.names_of([("salted", ["Crunchy Salted"]), ("near", ["Crunchy Saltedc"])], {})

    assert entities.match("Crunchy Saltedचा", names).how == "ambiguous"
    found = entities.match("Crunchy Saltedचा", names, MARATHI_ENDINGS)
    assert (found.target, found.how) == ("salted", "exact")


@pytest.mark.parametrize(
    ("text", "item", "how"),
    [
        ("लेझ", "lays", "exact"),
        ("लेझचा", "lays", "exact"),
        ("डोरिटोझचं", "doritos", "fuzzy"),  # near enough with its ending: never cut
        ("कॅम्पामध्ये", "campa", "exact"),
        ("सेव्हन अप", "seven_up", "exact"),
        ("क्रीम अँड ओनियन", "lays_cream_onion", "exact"),
        ("ॲपी फिझ", "appy_fizz", "exact"),
        ("अॅपी फिझ", "appy_fizz", "exact"),  # ॲ as two letters
        ("ऍपी फिझ", "appy_fizz", "exact"),  # and as the candra E letter
        ("अॅक्वाफिना", "aquafina", "exact"),
    ],
)
def test_the_demo_catalog_links_marathi_spellings(text: str, item: str, how: str) -> None:
    found = entities.match(text, PEPSICO.names, MARATHI_ENDINGS)

    assert (found.target, found.how) == (item, how)


def test_no_name_in_the_demo_catalog_is_two_items() -> None:
    # Batch T3 added Marathi spellings beside the Hindi ones: none may read as another item's.
    shared = {key: targets for key, targets in PEPSICO.names.by_key.items() if len(targets) > 1}

    assert shared == {}


@pytest.mark.parametrize("named", ["दिवाळी स्कीम", "कुरकुरेची दिवाळी स्कीम"])
def test_the_demo_pack_knows_diwali_as_marathi_writes_it(named: str) -> None:
    pack = pepsico()
    tally: Counter[str] = Counter()

    names = entities.scheme_names(pack.schemes)

    found = entities.link_scheme(names, named, tally)

    assert (found, dict(tally)) == ("kurkure_diwali_payout", {"scheme_by_alias": 1})
    assert entities.match(named, names).how == "exact"  # an alias, not a near guess
