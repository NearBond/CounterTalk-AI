"""Playbooks and org packs (BRIEF D14): versioned folders of YAML and CSV, loaded strictly.

- The playbook (playbooks/<id>/v<version>/) is the kind of visit, shared by every org: roles,
  insight kinds, severities, the category codebook and the prompts.
- The org pack (org_packs/<id>/v<version>/) is one company: what it sells, its teams and routing,
  and its catalog, schemes and distributors as CSVs.

Every error names the file and the field (PackError). IDs and versions are checked before any
path is built, and every path stays inside its own folder. The YAML loader refuses duplicate keys
and handles merge keys; models refuse unknown fields; CSVs need their exact header.
"""

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from countertalk.config import BACKEND_DIR
from countertalk.errors import Invalid

PLAYBOOKS_DIR = BACKEND_DIR / "playbooks"
ORG_PACKS_DIR = BACKEND_DIR / "org_packs"
FOLDER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
KEY = re.compile(r"[a-z][a-z0-9_]{0,62}")
CATALOG_HEADER = ["key", "company", "brand", "product", "aliases"]
SCHEME_HEADER = ["key", "name", "kind", "covers", "starts", "ends", "aliases"]
DISTRIBUTOR_HEADER = ["key", "name", "area"]
ALIAS_SEPARATOR = "|"

Key = Annotated[str, StringConstraints(pattern=f"^{KEY.pattern}$")]
FolderId = Annotated[str, StringConstraints(pattern=f"^{FOLDER_ID.pattern}$")]
Text = Annotated[str, StringConstraints(min_length=1)]
# What marks the same likely cause, for batch 15's issues.
GroupBy = Literal["sku", "competitor", "scheme", "distributor", "area"]
GROUP_BY_FIELDS: tuple[str, ...] = get_args(GroupBy)


class PackError(Invalid):
    """A playbook or org pack that doesn't check out: which file, which field, and why."""

    def __init__(self, path: Path, field: str, problem: str) -> None:
        # The folder, version and name: enough to find it, and the same on every machine.
        self.file = "/".join(path.parts[-3:])
        self.field = field
        super().__init__("invalid_pack", f"{self.file}: {field}: {problem}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Described(_Strict):
    description: Text


class Severity(_Strict):
    description: Text
    due_days: int = Field(ge=1)


class Exclusion(_Strict):
    text: Text
    category: Key


class Category(_Strict):
    name: Text
    definition: Text
    includes: list[Text] = Field(min_length=2, max_length=3)
    excludes: list[Exclusion]
    group_by: list[GroupBy] = Field(min_length=1)


class Playbook(_Strict):
    id: str
    version: int
    name: Text
    roles: dict[Key, Described]
    kinds: dict[Key, Described] = Field(min_length=1)
    # Highest first: an override's min_severity counts down this order.
    severities: dict[Key, Severity] = Field(min_length=1)
    competitor_reasons: dict[Key, Described] = Field(min_length=1)
    task_types: dict[Key, Described] = Field(min_length=1)
    task_owners: dict[Key, Described] = Field(min_length=1)
    categories: dict[Key, Category] = Field(min_length=1)
    prompt: Text
    roles_prompt: Text
    evals: Text


class PlaybookRef(_Strict):
    # Checked like any ID, before a path is built from it.
    id: FolderId
    version: int = Field(ge=1)


class Team(_Strict):
    name: Text
    contact: Text
    # What the team deals with: the AI's team suggestion reads it (batch 10).
    handles: Text


class Override(_Strict):
    """A category's second team, for insights at or above a severity (at most one per category)."""

    category: Key
    min_severity: Key
    team: Key


class CatalogFiles(_Strict):
    skus: Text
    competitors: Text
    schemes: Text
    distributors: Text


class OrgPackFile(_Strict):
    id: str
    version: int
    playbook: PlaybookRef
    display_name: Text
    # A few lines on what the org sells, for the extraction prompt.
    sells: Text
    teams: dict[Key, Team] = Field(min_length=1)
    routing: dict[Key, Key]
    overrides: list[Override]
    catalog: CatalogFiles


@dataclass(frozen=True)
class CatalogRow:
    key: str
    company: str
    brand: str
    # None for a brand as a whole.
    product: str | None
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Scheme:
    key: str
    name: str
    kind: str
    covers: tuple[str, ...]
    starts: date
    ends: date
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Distributor:
    key: str
    name: str
    area: str


@dataclass(frozen=True)
class OrgPack:
    """An org pack with its playbook and its CSVs, all checked."""

    file: OrgPackFile
    playbook: Playbook
    # Where the playbook was loaded from: its prompts are read from there too.
    playbook_folder: Path
    skus: tuple[CatalogRow, ...]
    competitors: tuple[CatalogRow, ...]
    schemes: tuple[Scheme, ...]
    distributors: tuple[Distributor, ...]

    @property
    def id(self) -> str:
        return self.file.id

    @property
    def version(self) -> int:
        return self.file.version


# --- reading files ---------------------------------------------------------------------------


class _DuplicateKey(Exception):
    def __init__(self, key: object, line: int) -> None:
        super().__init__(key, line)
        self.key, self.line = key, line


class _ComplexKey(Exception):
    def __init__(self, line: int) -> None:
        super().__init__(line)
        self.line = line


class _StrictLoader(yaml.SafeLoader):
    """yaml.safe_load, refusing a key written twice in one mapping. Merge keys (<<) still work:
    a key a merge brings in may be set again, which is what merging is for."""


def _mapping_without_duplicates(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[object] = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str | int | float | bool) and key is not None:
            raise _ComplexKey(key_node.start_mark.line + 1)  # a list or a mapping as a key
        if key in seen:
            raise _DuplicateKey(key, key_node.start_mark.line + 1)
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates
)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")  # a byte-order mark is accepted
    except FileNotFoundError:
        raise PackError(path, "(file)", "is missing") from None
    except UnicodeDecodeError:
        raise PackError(path, "(file)", "isn't UTF-8") from None
    except OSError as error:  # a folder where a file should be, or no permission
        raise PackError(path, "(file)", f"can't be read ({type(error).__name__})") from None


def read_yaml(path: Path) -> dict[str, Any]:
    """A YAML mapping, read strictly: a duplicate key, a key that isn't plain text or a file
    that isn't YAML is a PackError naming the file and the line. Batch 10's K2 files too."""
    try:
        data = yaml.load(_read_text(path), Loader=_StrictLoader)  # noqa: S506 - a SafeLoader
    except _DuplicateKey as error:
        raise PackError(path, str(error.key), f"is written twice (line {error.line})") from None
    except _ComplexKey as error:
        raise PackError(path, f"line {error.line}", "a key must be plain text") from None
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark is not None else "(file)"
        raise PackError(path, where, "isn't valid YAML") from None
    if not isinstance(data, dict):
        raise PackError(path, "(file)", "must be a mapping of fields")
    return data


def _model[T: BaseModel](model: type[T], data: dict[str, Any], path: Path) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise PackError(path, *first_problem(error)) from None


def first_problem(error: ValidationError) -> tuple[str, str]:
    """Where, as a dotted path ("(file)" for the whole), and what, of the first problem pydantic
    found: one line for a person, for every strictly read file (R1)."""
    first = error.errors()[0]
    return ".".join(str(part) for part in first["loc"]) or "(file)", first["msg"]


def _folder(root: Path, folder_id: str, version: int, path_for_errors: Path) -> Path:
    """<root>/<id>/v<version>, checked before the path is built."""
    if not isinstance(folder_id, str) or FOLDER_ID.fullmatch(folder_id) is None:
        raise PackError(path_for_errors, "id", f"{folder_id!r} isn't a valid ID")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise PackError(path_for_errors, "version", f"{version!r} isn't a version number")
    return root / folder_id / f"v{version}"


def _inside(folder: Path, relative: str, path: Path, field: str, *, must_exist: bool) -> Path:
    """A path a file names, which must stay inside that file's folder."""
    target = (folder / relative).resolve()
    if Path(relative).is_absolute() or not target.is_relative_to(folder.resolve()):
        raise PackError(path, field, f"{relative} is outside {folder.name}/")
    if must_exist and not target.is_file():
        raise PackError(path, field, f"{relative} doesn't exist")
    return target


# --- the playbook ------------------------------------------------------------------------------


def load_playbook(playbook_id: str, version: int, root: Path = PLAYBOOKS_DIR) -> Playbook:
    folder = _folder(root, playbook_id, version, root / str(playbook_id) / "playbook.yaml")
    path = folder / "playbook.yaml"
    playbook = _model(Playbook, read_yaml(path), path)
    if (playbook.id, playbook.version) != (playbook_id, version):
        raise PackError(path, "id", f"says {playbook.id} v{playbook.version}, not its folder's")
    if "other" not in playbook.roles:
        raise PackError(path, "roles", "must include other")
    for key, category in playbook.categories.items():
        if len(set(category.group_by)) != len(category.group_by):
            raise PackError(path, f"categories.{key}.group_by", "names a field twice")
        for index, exclusion in enumerate(category.excludes):
            if exclusion.category not in playbook.categories or exclusion.category == key:
                field = f"categories.{key}.excludes.{index}.category"
                raise PackError(path, field, f"{exclusion.category} isn't another category")
    _inside(folder, playbook.prompt, path, "prompt", must_exist=True)
    _inside(folder, playbook.roles_prompt, path, "roles_prompt", must_exist=True)
    _inside(folder, playbook.evals, path, "evals", must_exist=False)
    return playbook


def playbook_text(pack: "OrgPack", which: Literal["prompt", "roles_prompt"]) -> str:
    """The pack's playbook's prompt or roles prompt, from the folder it was loaded from."""
    folder = pack.playbook_folder
    relative = getattr(pack.playbook, which)
    return _read_text(_inside(folder, relative, folder / "playbook.yaml", which, must_exist=True))


# --- the org pack ------------------------------------------------------------------------------


def _rows(path: Path, header: list[str]) -> list[tuple[int, dict[str, str]]]:
    """(line number, row) for each non-blank line, under exactly this header."""
    # Fields may be quoted, with commas and line breaks: the reader sees the whole text.
    reader = csv.reader(io.StringIO(_read_text(path), newline=""))
    try:
        lines = [(reader.line_num, cells) for cells in reader]
    except csv.Error:
        raise PackError(path, f"line {reader.line_num}", "isn't valid CSV") from None
    if not lines or [cell.strip() for cell in lines[0][1]] != header:
        raise PackError(path, "header", f"must be exactly {','.join(header)}")
    rows = []
    for number, cells in lines[1:]:
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) != len(header):
            raise PackError(path, f"line {number}", f"has {len(cells)} columns, not {len(header)}")
        row = dict(zip(header, (cell.strip() for cell in cells), strict=True))
        if KEY.fullmatch(row["key"]) is None:
            raise PackError(path, f"line {number}: key", f"{row['key']!r} isn't a valid key")
        rows.append((number, row))
    return rows


def _required(path: Path, number: int, row: dict[str, str], *fields: str) -> None:
    for field in fields:
        if not row[field]:
            raise PackError(path, f"line {number}: {field}", "is empty")


def _aliases(text: str) -> tuple[str, ...]:
    found = [alias.strip() for alias in text.split(ALIAS_SEPARATOR)]
    return tuple(dict.fromkeys(alias for alias in found if alias))


def _unique(path: Path, rows: Iterable[tuple[int, dict[str, str]]], seen: dict[str, str]) -> None:
    """Keys are unique across the catalog's files: they key the org's catalog (org_id, key)."""
    for number, row in rows:
        if row["key"] in seen:
            raise PackError(path, f"line {number}: key", f"{row['key']} repeats {seen[row['key']]}")
        seen[row["key"]] = f"{path.name} line {number}"


def _catalog(path: Path, seen: dict[str, str]) -> tuple[CatalogRow, ...]:
    rows = _rows(path, CATALOG_HEADER)
    _unique(path, rows, seen)
    for number, row in rows:
        _required(path, number, row, "company", "brand")
    return tuple(
        CatalogRow(
            row["key"],
            row["company"],
            row["brand"],
            row["product"] or None,
            _aliases(row["aliases"]),
        )
        for _, row in rows
    )


def _date(path: Path, number: int, field: str, text: str) -> date:
    if ISO_DATE.fullmatch(text) is None:
        raise PackError(path, f"line {number}: {field}", f"{text!r} isn't a date like 2026-11-01")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise PackError(path, f"line {number}: {field}", f"{text} isn't a real date") from None


def _schemes(path: Path, catalog_keys: set[str]) -> tuple[Scheme, ...]:
    rows = _rows(path, SCHEME_HEADER)
    _unique(path, rows, {})
    schemes = []
    for number, row in rows:
        _required(path, number, row, "name", "kind", "covers")
        covers = tuple(key.strip() for key in row["covers"].split(ALIAS_SEPARATOR) if key.strip())
        for key in covers:
            if key not in catalog_keys:
                raise PackError(path, f"line {number}: covers", f"{key} isn't a key in the catalog")
        starts = _date(path, number, "starts", row["starts"])
        ends = _date(path, number, "ends", row["ends"])
        if ends < starts:
            raise PackError(path, f"line {number}: ends", "is before it starts")
        schemes.append(
            Scheme(
                row["key"], row["name"], row["kind"], covers, starts, ends, _aliases(row["aliases"])
            )
        )
    return tuple(schemes)


def _distributors(path: Path) -> tuple[Distributor, ...]:
    rows = _rows(path, DISTRIBUTOR_HEADER)
    _unique(path, rows, {})
    for number, row in rows:
        _required(path, number, row, "name", "area")
    return tuple(Distributor(row["key"], row["name"], row["area"]) for _, row in rows)


def _check_routing(file: OrgPackFile, playbook: Playbook, path: Path) -> None:
    where = f"{playbook.id} v{playbook.version}"
    for category, team in file.routing.items():
        if category not in playbook.categories:
            raise PackError(path, f"routing.{category}", f"isn't a category of {where}")
        if team not in file.teams:
            raise PackError(path, f"routing.{category}", f"{team} isn't one of the pack's teams")
    overridden: set[str] = set()
    for index, override in enumerate(file.overrides):
        field = f"overrides.{index}"
        if override.category not in playbook.categories:
            raise PackError(path, f"{field}.category", f"isn't a category of {where}")
        if override.category in overridden:
            # An insight has at most 2 teams (batch 12): its category's team and one override.
            raise PackError(
                path, f"{field}.category", f"{override.category} already has an override"
            )
        overridden.add(override.category)
        if override.min_severity not in playbook.severities:
            raise PackError(path, f"{field}.min_severity", f"isn't a severity of {where}")
        if override.team not in file.teams:
            raise PackError(path, f"{field}.team", f"{override.team} isn't one of the pack's teams")
        if file.routing.get(override.category) == override.team:
            raise PackError(path, f"{field}.team", "is already the category's team")


def load_org_pack(
    pack_id: str, version: int, root: Path = ORG_PACKS_DIR, playbooks: Path = PLAYBOOKS_DIR
) -> OrgPack:
    folder = _folder(root, pack_id, version, root / str(pack_id) / "org_pack.yaml")
    path = folder / "org_pack.yaml"
    file = _model(OrgPackFile, read_yaml(path), path)
    if (file.id, file.version) != (pack_id, version):
        raise PackError(path, "id", f"says {file.id} v{file.version}, not its folder's")
    ref = file.playbook
    playbook_folder = _folder(playbooks, ref.id, ref.version, path)  # checked as an ID already
    if not playbook_folder.is_dir():
        raise PackError(path, "playbook", f"{ref.id} v{ref.version} doesn't exist")
    playbook = load_playbook(ref.id, ref.version, playbooks)
    _check_routing(file, playbook, path)
    seen: dict[str, str] = {}
    skus = _catalog(_inside(folder, file.catalog.skus, path, "catalog.skus", must_exist=True), seen)
    competitors = _catalog(
        _inside(folder, file.catalog.competitors, path, "catalog.competitors", must_exist=True),
        seen,
    )
    schemes = _schemes(
        _inside(folder, file.catalog.schemes, path, "catalog.schemes", must_exist=True),
        {row.key for row in skus},
    )
    distributors = _distributors(
        _inside(folder, file.catalog.distributors, path, "catalog.distributors", must_exist=True)
    )
    return OrgPack(file, playbook, playbook_folder, skus, competitors, schemes, distributors)


# --- what the rest of the app reads ----------------------------------------------------------


@dataclass(frozen=True)
class Route:
    category: str
    team: str | None
    override: Override | None


def routing_table(pack: OrgPack) -> list[Route]:
    """Each playbook category, its team (None: it goes to triage) and its override, in order."""
    overrides = {override.category: override for override in pack.file.overrides}
    return [
        Route(category, pack.file.routing.get(category), overrides.get(category))
        for category in pack.playbook.categories
    ]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """A field that may be null. The one place this is written, so a switch to anyOf (if Muse
    Spark's strict mode refuses type lists, batch 9) is a one-line change."""
    nullable = {**schema, "type": [schema["type"], "null"]}
    if "enum" in schema:
        nullable["enum"] = [*schema["enum"], None]
    return nullable


def _enum(keys: Iterable[str]) -> dict[str, Any]:
    return {"type": "string", "enum": list(keys)}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    # Strict mode: every key required, and nothing else allowed.
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def _text(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _list(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


def build_extraction_schema(playbook: Playbook, pack: OrgPack) -> dict[str, Any]:
    """The JSON schema extraction must follow (batch 10), built per org: the team and scheme
    enums are the org pack's own. Strict mode's rules: no oneOf or allOf, every key required,
    additionalProperties false everywhere, and $defs for evidence."""
    if (playbook.id, playbook.version) != (pack.playbook.id, pack.playbook.version):
        raise ValueError("the playbook must be the one the org pack was checked against")
    evidence = _object(
        {
            "turn_id": {"type": "integer", "description": "The transcript turn the quote is from."},
            "quote": _text("Copied exactly from that turn, in its own script, 40 words or fewer."),
        }
    )
    insight = _object(
        {
            "kind": _enum(playbook.kinds),
            "category": _enum(playbook.categories),
            "severity": _enum(playbook.severities),
            "summary_en": _text("One sentence in English."),
            "evidence": _list({"$ref": "#/$defs/evidence"}),
            "sku_mentions": _list(_text("A product or brand of the org's, as said.")),
            "competitor_mentions": _list(_text("A competitor's product or brand, as said.")),
            "competitor_reason": _nullable(_enum(playbook.competitor_reasons)),
            "scheme_key": _nullable(_enum(scheme.key for scheme in pack.schemes)),
            "suggested_team": _nullable(_enum(pack.file.teams)),
            "team_reason": _nullable(_text("Why that team, in 20 words or fewer.")),
        }
    )
    task = _object(
        {
            "type": _enum(playbook.task_types),
            "description_en": _text("What's to be done, in English."),
            "due": _nullable(_text("YYYY-MM-DD, if a date was said.")),
            "owner": _enum(playbook.task_owners),
        }
    )
    order_item = _object(
        {
            "name": _text("The product as said, in its own script."),
            "quantity": _nullable({"type": "number", "description": "How many, if it was said."}),
            "unit": _nullable(_text("As said (dabba, peti, packet), if one was.")),
        }
    )
    schema = _object(
        {
            "summary_en": _text("The visit in two or three sentences, in English."),
            "next_visit_date": _nullable(_text("YYYY-MM-DD, if a date was said.")),
            "insights": _list(insight),
            "tasks": _list(task),
            "order_items": _list(order_item),
        }
    )
    return {**schema, "$defs": {"evidence": evidence}}
