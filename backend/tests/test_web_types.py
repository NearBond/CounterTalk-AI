"""web/src/types/api.ts mirrors the API's schemas (CODING_GUIDELINES §2.11, §4.4).

The types are written by hand, so this checks every mirrored interface has exactly the
schema's fields: a field added, removed or renamed on one side only fails here.
"""

import re

import pytest
from pydantic import BaseModel

from countertalk.api.health import HealthOut
from countertalk.api.shops import ShopOut
from countertalk.api.visits import CompleteIn, SegmentOut, VisitIn, VisitOut
from countertalk.config import BACKEND_DIR

TYPES = BACKEND_DIR.parent / "web" / "src" / "types" / "api.ts"

MIRRORS: dict[str, type[BaseModel]] = {
    "Health": HealthOut,
    "Shop": ShopOut,
    "Visit": VisitOut,
    "NewVisit": VisitIn,
    "CompleteVisit": CompleteIn,
    "Segment": SegmentOut,
}


def interface_fields(source: str, name: str) -> set[str]:
    found = re.search(rf"export interface {name} \{{(.*?)\n\}}", source, re.DOTALL)
    assert found is not None, f"types/api.ts has no interface {name}"
    return set(re.findall(r"^\s+(\w+)\??:", found.group(1), re.MULTILINE))


@pytest.mark.parametrize("name", MIRRORS)
def test_the_web_type_has_exactly_the_schemas_fields(name: str) -> None:
    source = TYPES.read_text(encoding="utf-8")

    assert interface_fields(source, name) == set(MIRRORS[name].model_fields)
