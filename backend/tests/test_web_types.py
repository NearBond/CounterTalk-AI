"""web/src/types/api.ts mirrors the API's schemas (CODING_GUIDELINES §2.11, §4.4).

The types are written by hand, so this checks every mirrored interface has exactly the
schema's fields: a field added, removed or renamed on one side only fails here.
"""

import re

import pytest
from pydantic import BaseModel

from countertalk import limits, storage
from countertalk.api.health import HealthOut
from countertalk.api.insights import (
    ChoiceOut,
    InsightChange,
    InsightOut,
    NewInsight,
    NewNote,
    NoteOut,
    QuoteOut,
    ReportOut,
    RouteOut,
    TaskOut,
)
from countertalk.api.shops import ShopOut
from countertalk.api.visits import (
    CompleteIn,
    ReviewOut,
    SegmentOut,
    TranscriptOut,
    TurnOut,
    VisitIn,
    VisitOut,
)
from countertalk.config import BACKEND_DIR

TYPES = BACKEND_DIR.parent / "web" / "src" / "types" / "api.ts"

MIRRORS: dict[str, type[BaseModel]] = {
    "Health": HealthOut,
    "Shop": ShopOut,
    "Visit": VisitOut,
    "NewVisit": VisitIn,
    "CompleteVisit": CompleteIn,
    "Segment": SegmentOut,
    "Transcript": TranscriptOut,
    "Turn": TurnOut,
    "Quote": QuoteOut,
    "Route": RouteOut,
    "Insight": InsightOut,
    "InsightChange": InsightChange,
    "NewInsight": NewInsight,
    "NewNote": NewNote,
    "Note": NoteOut,
    "Report": ReportOut,
    "Task": TaskOut,
    "Choice": ChoiceOut,
    "Review": ReviewOut,
}


def interface_fields(source: str, name: str) -> set[str]:
    found = re.search(rf"export interface {name} \{{(.*?)\n\}}", source, re.DOTALL)
    assert found is not None, f"types/api.ts has no interface {name}"
    return set(re.findall(r"^\s+(\w+)\??:", found.group(1), re.MULTILINE))


@pytest.mark.parametrize("name", MIRRORS)
def test_the_web_type_has_exactly_the_schemas_fields(name: str) -> None:
    source = TYPES.read_text(encoding="utf-8")

    assert interface_fields(source, name) == set(MIRRORS[name].model_fields)


def test_the_web_app_mirrors_the_servers_upload_limits() -> None:
    # The Upload panel cuts files at the server's segment limit, and offers only the types it
    # takes; the server refuses anything else.
    source = TYPES.read_text(encoding="utf-8")
    exts = re.search(r"export const ALLOWED_EXTS = \[(.*?)\] as const;", source)
    limit = re.search(r"export const MAX_SEGMENT_BYTES = ([\d_]+);", source)

    assert exts is not None and limit is not None
    assert tuple(re.findall(r'"(\w+)"', exts.group(1))) == storage.ALLOWED_EXTS
    assert int(limit.group(1)) == limits.MAX_SEGMENT_BYTES


def test_the_web_app_mirrors_the_note_limit() -> None:
    source = TYPES.read_text(encoding="utf-8")
    limit = re.search(r"export const NOTE_MAX_CHARS = ([\d_]+);", source)

    assert limit is not None and int(limit.group(1)) == limits.NOTE_MAX_CHARS


def test_the_web_app_mirrors_the_summary_limit() -> None:
    source = TYPES.read_text(encoding="utf-8")
    limit = re.search(r"export const SUMMARY_MAX_CHARS = ([0-9_]+);", source)

    assert limit is not None and int(limit.group(1)) == limits.SUMMARY_MAX_CHARS
