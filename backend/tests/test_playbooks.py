"""playbooks.py: loading and checking playbooks and org packs, the schema and routing (batch 8)."""

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from countertalk.playbooks import (
    PLAYBOOKS_DIR,
    PackError,
    Route,
    build_extraction_schema,
    load_org_pack,
    load_playbook,
    playbook_text,
    routing_table,
)
from tests.conftest import ORG_PACK_FIXTURES, edit_file

CATEGORIES = (
    "schemes",
    "pricing",
    "supply",
    "damage",
    "payments",
    "quality",
    "demand",
    "servicing",
    "visibility",
    "praise",
    "other",
)


def test_the_playbook_is_appendix_as_codebook() -> None:
    playbook = load_playbook("fmcg-kirana-visit", 1)

    assert tuple(playbook.categories) == CATEGORIES
    assert tuple(playbook.roles) == ("rep", "shopkeeper", "other")
    assert {key: s.due_days for key, s in playbook.severities.items()} == {
        "high": 2,
        "medium": 7,
        "low": 14,
    }
    pack = load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)
    assert playbook_text(pack, "roles_prompt").count("\n- ") == 2  # one bullet per role


def test_a_second_org_pack_loads_from_the_same_code() -> None:
    pack = load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)

    assert (pack.id, pack.version, pack.playbook.id) == ("tiny-demo", 1, "fmcg-kirana-visit")
    assert list(pack.file.teams) == ["money", "field"]
    assert [row.key for row in pack.skus + pack.competitors] == ["crunchy", "crunchy_salt", "rival"]
    assert pack.skus[0].product is None and pack.skus[0].aliases == ("crunchy", "क्रंची")
    assert [(s.key, s.covers, str(s.starts), str(s.ends)) for s in pack.schemes] == [
        ("crunchy_winter", ("crunchy",), "2026-11-01", "2026-12-31")
    ]
    assert [(d.key, d.area) for d in pack.distributors] == [("t1", "North"), ("t2", "South")]


def test_merge_keys_are_followed_and_may_be_overridden(pack_roots: tuple[Path, Path]) -> None:
    packs, books = pack_roots
    edit_file(
        packs / "tiny-demo/v1/org_pack.yaml",
        "  money:\n    name: Money desk",
        "  money: &money\n    name: Money desk",
    )
    edit_file(
        packs / "tiny-demo/v1/org_pack.yaml",
        "  field:\n    name: Field team\n    contact: field@example.com\n",
        "  field:\n    <<: *money\n    name: Field team\n",
    )

    field = load_org_pack("tiny-demo", 1, packs, books).file.teams["field"]

    assert (field.name, field.contact, field.handles) == (
        "Field team",
        "money@example.com",
        "Deliveries and the shops' visits.",
    )


def test_a_byte_order_mark_is_accepted(pack_roots: tuple[Path, Path]) -> None:
    packs, books = pack_roots
    skus = packs / "tiny-demo/v1/skus.csv"
    skus.write_bytes(b"\xef\xbb\xbf" + skus.read_bytes())

    assert len(load_org_pack("tiny-demo", 1, packs, books).skus) == 2


PACK, BOOK = "tiny-demo/v1", "fmcg-kirana-visit/v1"
SECOND_OVERRIDE = (
    "    team: field\n  - category: payments\n    min_severity: medium\n    team: field\n"
)


@pytest.mark.parametrize(
    ("where", "old", "new", "file", "field"),
    [
        # §9.6: duplicate keys and unknown fields
        (
            f"{PACK}/org_pack.yaml",
            "  supply: field\n",
            "  supply: field\n  supply: money\n",
            "org_pack.yaml",
            "supply",
        ),
        (
            f"{BOOK}/playbook.yaml",
            "  quality:\n    name:",
            "  quality:\n    nmae: x\n    name:",
            "playbook.yaml",
            "categories.quality.nmae",
        ),
        # broken references
        (
            f"{PACK}/org_pack.yaml",
            "  supply: field",
            "  stock: field",
            "org_pack.yaml",
            "routing.stock",
        ),
        (
            f"{PACK}/org_pack.yaml",
            "  supply: field",
            "  supply: logistics",
            "org_pack.yaml",
            "routing.supply",
        ),
        (f"{PACK}/org_pack.yaml", "version: 1}", "version: 9}", "org_pack.yaml", "playbook"),
        (
            f"{PACK}/org_pack.yaml",
            "{id: fmcg-kirana-visit,",
            "{id: ../fmcg-kirana-visit,",
            "org_pack.yaml",
            "playbook.id",
        ),
        (
            f"{PACK}/org_pack.yaml",
            "routing:\n",
            "routing:\n  ? [a, b]\n  : field\n",
            "org_pack.yaml",
            "line 17",
        ),
        # routing: at most one override per category, and never the category's own team
        (
            f"{PACK}/org_pack.yaml",
            "    team: field\n",
            SECOND_OVERRIDE,
            "org_pack.yaml",
            "overrides.1.category",
        ),
        (
            f"{PACK}/org_pack.yaml",
            "    team: field\n",
            "    team: money\n",
            "org_pack.yaml",
            "overrides.0.team",
        ),
        # the codebook
        (
            f"{BOOK}/playbook.yaml",
            "    group_by: [distributor]\n  quality:",
            "    group_by: [shop]\n  quality:",
            "playbook.yaml",
            "categories.payments.group_by.0",
        ),
        (
            f"{BOOK}/playbook.yaml",
            "        category: visibility\n",
            "        category: displays\n",
            "playbook.yaml",
            "categories.schemes.excludes.1.category",
        ),
        (
            f"{BOOK}/playbook.yaml",
            "  other:\n    description: Anyone else",
            "  bystander:\n    description: Anyone else",
            "playbook.yaml",
            "roles",
        ),
        # paths stay inside their folder
        (
            f"{BOOK}/playbook.yaml",
            "prompt: prompt.md",
            "prompt: ../../../secret.md",
            "playbook.yaml",
            "prompt",
        ),
        (
            f"{PACK}/org_pack.yaml",
            "skus: skus.csv",
            "skus: ../../../skus.csv",
            "org_pack.yaml",
            "catalog.skus",
        ),
        # CSVs
        (f"{PACK}/skus.csv", "key,company,brand", "key,brand", "skus.csv", "header"),
        (f"{PACK}/competitors.csv", "\nrival,", "\ncrunchy,", "competitors.csv", "line 2: key"),
        (f"{PACK}/schemes.csv", "2026-12-31", "31/12/2026", "schemes.csv", "line 2: ends"),
        (
            f"{PACK}/schemes.csv",
            "payout,crunchy,",
            "payout,munchy,",
            "schemes.csv",
            "line 2: covers",
        ),
    ],
    ids=[
        "duplicate-key",
        "unknown-field",
        "unknown-category",
        "unknown-team",
        "missing-playbook",
        "playbook-ref-not-an-id",
        "key-not-plain-text",
        "second-override",
        "override-repeats-team",
        "unknown-group-by",
        "excludes-missing-category",
        "no-other-role",
        "prompt-outside",
        "csv-outside",
        "csv-header",
        "repeated-key",
        "bad-date",
        "unknown-cover",
    ],
)
def test_a_broken_pack_names_the_file_and_the_field(
    pack_roots: tuple[Path, Path], where: str, old: str, new: str, file: str, field: str
) -> None:
    packs, books = pack_roots
    edit_file((books if where.startswith(BOOK) else packs) / where, old, new)

    with pytest.raises(PackError) as error:
        load_org_pack("tiny-demo", 1, packs, books)

    folder = "fmcg-kirana-visit" if file == "playbook.yaml" else "tiny-demo"
    assert (error.value.file, error.value.field) == (f"{folder}/v1/{file}", field)


@pytest.mark.parametrize(
    ("pack_id", "version"), [("../tiny-demo", 1), ("tiny-demo", 0), ("Tiny", 1)]
)
def test_an_id_or_version_is_checked_before_any_path(pack_id: str, version: int) -> None:
    with pytest.raises(PackError) as error:
        load_org_pack(pack_id, version, ORG_PACK_FIXTURES)

    assert error.value.field == ("version" if version == 0 else "id")


def objects(schema: Any) -> Iterator[dict[str, Any]]:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from objects(value)


def test_the_schema_follows_strict_modes_rules() -> None:
    pack = load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)

    schema = build_extraction_schema(pack.playbook, pack)

    assert all(
        o["additionalProperties"] is False and o["required"] == list(o["properties"])
        for o in objects(schema)
    )
    assert "oneOf" not in repr(schema) and "allOf" not in repr(schema)
    insight = schema["properties"]["insights"]["items"]["properties"]
    assert insight["category"]["enum"] == list(CATEGORIES)
    assert insight["evidence"]["items"] == {"$ref": "#/$defs/evidence"}
    assert insight["competitor_reason"]["type"] == ["string", "null"]
    assert insight["competitor_reason"]["enum"][-1] is None


def test_each_orgs_schema_allows_only_its_own_teams_and_schemes(
    pack_roots: tuple[Path, Path],
) -> None:
    packs, books = pack_roots
    other = packs / "other-demo"
    shutil.copytree(packs / "tiny-demo", other)
    edit_file(other / "v1/org_pack.yaml", "id: tiny-demo", "id: other-demo")
    edit_file(other / "v1/org_pack.yaml", "  money:", "  accounts:")
    edit_file(other / "v1/org_pack.yaml", "  payments: money", "  payments: accounts")
    edit_file(other / "v1/schemes.csv", "crunchy_winter,", "crunchy_summer,")

    tiny, second = (
        load_org_pack(pack_id, 1, packs, books) for pack_id in ("tiny-demo", "other-demo")
    )

    def enums(pack: Any) -> tuple[list[Any], list[Any]]:
        insight = build_extraction_schema(pack.playbook, pack)["properties"]["insights"]["items"]
        return insight["properties"]["suggested_team"]["enum"], insight["properties"]["scheme_key"][
            "enum"
        ]

    assert enums(tiny) == (["money", "field", None], ["crunchy_winter", None])
    assert enums(second) == (["accounts", "field", None], ["crunchy_summer", None])


def test_the_routing_table_has_every_category_its_team_and_override() -> None:
    pack = load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)

    table = routing_table(pack)

    assert [route.category for route in table] == list(CATEGORIES)
    payments = next(route for route in table if route.category == "payments")
    assert (payments.team, payments.override and payments.override.team) == ("money", "field")
    assert [route.category for route in table if route.team is None] == [
        c for c in CATEGORIES if c not in ("payments", "supply")
    ]
    assert isinstance(table[0], Route)


def test_the_test_orgs_pack_fits_k6() -> None:
    pack = load_org_pack("pepsico-india-demo", 1)

    # The planted six-week world (BUILD_PLAN K6) seeds on this pack.
    assert list(pack.file.teams) == [
        "finance",
        "distribution",
        "trade_marketing",
        "sales_ops",
        "area_manager",
    ]
    assert all(team.contact.endswith("@example.com") for team in pack.file.teams.values())
    # As the user decided on 1 Nov: schemes to finance (K6 P1), and every category but other
    # gets the nearest of the five teams.
    assert {
        route.category: (route.team, route.override and route.override.min_severity)
        for route in routing_table(pack)
    } == {
        "schemes": ("finance", None),
        "pricing": ("sales_ops", None),
        "supply": ("distribution", None),
        "damage": ("distribution", None),
        "payments": ("finance", "high"),
        "quality": ("distribution", None),
        "demand": ("trade_marketing", None),
        "servicing": ("sales_ops", None),
        "visibility": ("trade_marketing", None),
        "praise": ("area_manager", None),
        "other": (None, None),
    }
    assert [(o.category, o.team) for o in pack.file.overrides] == [("payments", "area_manager")]
    assert [d.key for d in pack.distributors] == ["d1", "d2", "d3"]
    assert len({d.area for d in pack.distributors}) == 2
    assert all(d.name.endswith("(example)") for d in pack.distributors)
    diwali = next(s for s in pack.schemes if s.key == "kurkure_diwali_payout")
    assert {"Diwali scheme", "दिवाली स्कीम", "Kurkure scheme"} <= set(diwali.aliases)
    # rp06's pending display payments are for August and September, and rp03's for "last
    # quarter": the display scheme covers them.
    rack = next(s for s in pack.schemes if s.key == "lays_display_rack")
    assert (rack.starts.isoformat(), rack.ends.isoformat()) == ("2026-07-01", "2026-12-31")
    assert max(s.ends for s in pack.schemes).isoformat() >= "2026-12-31"


def test_every_brand_has_a_devanagari_and_a_latin_alias() -> None:
    pack = load_org_pack("pepsico-india-demo", 1)

    brands = [row for row in pack.skus + pack.competitors if row.product is None]
    devanagari = [row.key for row in brands if any("ऀ" <= c <= "ॿ" for a in row.aliases for c in a)]
    latin = [row.key for row in brands if any(a.isascii() for a in row.aliases)]

    assert devanagari == latin == [row.key for row in brands]
    header = (PLAYBOOKS_DIR.parent / "org_packs/pepsico-india-demo/v1/org_pack.yaml").read_text(
        encoding="utf-8"
    )
    assert "not a customer or partner" in header.split("\nid:")[0]


def test_a_quoted_csv_field_may_hold_commas_and_line_breaks(pack_roots: tuple[Path, Path]) -> None:
    packs, books = pack_roots
    edit_file(
        packs / "tiny-demo/v1/distributors.csv",
        "t2,South Traders (example),South",
        't2,"South Traders, Unit 2\n(example)",South',
    )

    pack = load_org_pack("tiny-demo", 1, packs, books)

    assert [d.name for d in pack.distributors] == [
        "North Traders (example)",
        "South Traders, Unit 2\n(example)",
    ]


def test_the_prompts_come_from_the_playbook_the_pack_was_loaded_with(
    pack_roots: tuple[Path, Path],
) -> None:
    packs, books = pack_roots
    (books / "fmcg-kirana-visit/v1/roles_prompt.md").write_text("- edited\n", encoding="utf-8")

    pack = load_org_pack("tiny-demo", 1, packs, books)

    assert playbook_text(pack, "roles_prompt") == "- edited\n"


# --- languages (batch T3) ---------------------------------------------------------------------


def test_a_pack_naming_no_languages_has_hindi_and_english() -> None:
    assert load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES).languages == ("hi", "en")
    assert load_org_pack("pepsico-india-demo", 1).languages == ("hi", "en")  # as before T3


def test_a_pack_s_own_languages_are_kept_in_order(pack_roots: tuple[Path, Path]) -> None:
    packs, books = pack_roots
    edit_file(packs / PACK / "org_pack.yaml", "catalog:\n", "languages: [mr, hi, en]\ncatalog:\n")

    assert load_org_pack("tiny-demo", 1, packs, books).languages == ("mr", "hi", "en")


@pytest.mark.parametrize(
    ("named", "field", "problem"),
    [
        ("[ta]", "languages.0", "Input should be 'hi', 'en' or 'mr'"),
        ("[]", "languages", "List should have at least 1 item after validation, not 0"),
        ("[mr, hi, mr]", "languages", "mr named twice"),
        ("mr", "languages", "Input should be a valid list"),
    ],
    ids=["unknown", "empty", "twice", "not-a-list"],
)
def test_a_pack_s_languages_are_checked(
    pack_roots: tuple[Path, Path], named: str, field: str, problem: str
) -> None:
    packs, books = pack_roots
    edit_file(packs / PACK / "org_pack.yaml", "catalog:\n", f"languages: {named}\ncatalog:\n")

    with pytest.raises(PackError) as error:
        load_org_pack("tiny-demo", 1, packs, books)

    assert str(error.value) == f"invalid_pack: {PACK}/org_pack.yaml: {field}: {problem}"


def test_the_maharashtra_pack_is_the_india_pack_with_marathi_spellings_and_its_own_area() -> None:
    india = load_org_pack("pepsico-india-demo", 1)
    maharashtra = load_org_pack("pepsico-maharashtra-demo", 1)

    assert maharashtra.languages == ("mr", "hi", "en")
    assert maharashtra.file.display_name == "PepsiCo India, Maharashtra (example)"
    same = ("playbook", "sells", "teams", "routing", "overrides")
    assert [getattr(maharashtra.file, f) for f in same] == [getattr(india.file, f) for f in same]
    assert maharashtra.schemes == india.schemes
    assert [(d.key, d.area) for d in maharashtra.distributors] == [
        ("d1", "Pune"),
        ("d2", "Pune"),
        ("d3", "Nashik"),
    ]
    for theirs, ours in (
        (india.skus, maharashtra.skus),
        (india.competitors, maharashtra.competitors),
    ):
        assert [(r.key, r.company, r.brand, r.product) for r in ours] == [
            (r.key, r.company, r.brand, r.product) for r in theirs
        ]
        assert all(set(a.aliases) <= set(b.aliases) for a, b in zip(theirs, ours, strict=True))
    added = {
        r.key: sorted(set(r.aliases) - set(old.aliases))
        for old, r in zip(
            (*india.skus, *india.competitors),
            (*maharashtra.skus, *maharashtra.competitors),
            strict=True,
        )
        if set(r.aliases) != set(old.aliases)
    }
    assert added == {
        "lays": ["लेझ"],
        "lays_cream_onion": ["क्रीम अँड ओनियन"],
        "lays_hot_n_sweet": ["हॉट अँड स्वीट"],
        "doritos": ["डोरिटोझ"],
        "doritos_nacho_cheese": ["नाचो चीझ"],
        "cheetos": ["चीटोझ"],
        "cheetos_cheez_puffs": ["चीझ पफ्स"],
        "seven_up": ["सेव्हन अप"],
        "mountain_dew": ["माउंटन ड्यू"],
        "nimbooz": ["निंबूझ"],
        "aquafina": ["ॲक्वाफिना"],
        "campa": ["कॅम्पा"],
        "campa_cola": ["कॅम्पा कोला"],
        "appy_fizz": ["ॲपी फिझ"],
    }
    # Marathi for "my", so never Maaza's alias in this pack.
    assert all("माझा" not in r.aliases for r in (*maharashtra.skus, *maharashtra.competitors))
