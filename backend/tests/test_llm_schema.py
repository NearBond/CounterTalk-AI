"""llm/schema.py: checking an answer against the strict JSON schema it was asked for (batch 9).

Strict mode should make Meta's answers match, but an answer is outside data (R4): a mismatch gets
one repair call, so the check names what's wrong.
"""

from typing import Any

import pytest

from countertalk.llm.schema import problem
from countertalk.playbooks import build_extraction_schema, load_org_pack
from tests.conftest import ORG_PACK_FIXTURES

SPEAKER = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["c0:A", "c0:B"]},
        "role": {"type": "string"},
        "confidence": {"type": ["number", "null"]},
        "turns": {"type": "array", "items": {"type": "integer"}},
        "sure": {"type": "boolean"},
    },
    "required": ["label", "role", "confidence", "turns", "sure"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {"speakers": {"type": "array", "items": {"$ref": "#/$defs/speaker"}}},
    "required": ["speakers"],
    "additionalProperties": False,
    "$defs": {"speaker": SPEAKER},
}
GOOD = {"label": "c0:A", "role": "rep", "confidence": 0.9, "turns": [0, 3], "sure": True}


def speakers(**changes: Any) -> dict[str, Any]:
    return {"speakers": [{**GOOD, **changes}]}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (speakers(), None),
        (speakers(confidence=None), None),
        (speakers(confidence=1), None),
        ({"speakers": []}, None),
        (speakers(label="c9:Z"), "speakers[0].label: 'c9:Z' isn't one of the allowed values"),
        (speakers(role=7), "speakers[0].role: should be string, not integer"),
        (
            speakers(confidence="high"),
            "speakers[0].confidence: should be number or null, not string",
        ),
        (
            speakers(confidence=True),
            "speakers[0].confidence: should be number or null, not boolean",
        ),
        (speakers(turns=[1, 2.5]), "speakers[0].turns[1]: should be integer, not number"),
        (speakers(turns=[True]), "speakers[0].turns[0]: should be integer, not boolean"),
        (speakers(sure=1), "speakers[0].sure: should be boolean, not integer"),
        (speakers(turns="0,3"), "speakers[0].turns: should be array, not string"),
        ({"speakers": [GOOD, "rep"]}, "speakers[1]: should be object, not string"),
        (
            {"speakers": [{k: v for k, v in GOOD.items() if k != "role"}]},
            "speakers[0]: role is missing",
        ),
        (speakers(mood="calm"), "speakers[0]: mood isn't expected"),
        ({}, "speakers is missing"),
        ([], "should be object, not array"),
    ],
)
def test_an_answer_is_checked_against_its_schema(value: object, expected: str | None) -> None:
    assert problem(value, SCHEMA) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("rep", None),
        (3, "should match one of its forms"),
    ],
)
def test_any_of_takes_any_one_of_its_forms(value: object, expected: str | None) -> None:
    schema = {"anyOf": [{"type": "string", "enum": ["rep", "other"]}, {"type": "null"}]}

    assert problem(value, schema) == expected


def test_a_schema_keyword_it_cant_check_is_a_bug_not_a_pass() -> None:
    # Checked over the whole schema, even the parts this answer doesn't reach.
    schema = {"type": ["array", "null"], "items": {"type": "string", "pattern": "^a"}}

    with pytest.raises(ValueError) as error:
        problem(None, schema)

    assert str(error.value) == "can't check the schema keyword pattern"


@pytest.mark.parametrize(
    ("schema", "why"),
    [
        ({"type": ["string", "date"]}, "unknown type date"),
        (
            {"items": {"$ref": "#/$defs/missing"}},
            "the schema has no $defs entry for #/$defs/missing",
        ),
        ({"additionalProperties": True}, "additionalProperties must be false"),
        ({"anyOf": "string"}, "anyOf must be a list of schemas"),
    ],
)
def test_a_schema_it_cant_check_is_found_whatever_the_answer(
    schema: dict[str, Any], why: str
) -> None:
    with pytest.raises(ValueError) as error:
        problem([], schema)

    assert str(error.value) == why


def test_the_extraction_schema_uses_only_keywords_it_checks() -> None:
    pack = load_org_pack("tiny-demo", 1, ORG_PACK_FIXTURES)
    empty = {
        "insights": [],
        "tasks": [],
        "summary_en": "",
        "next_visit_date": None,
        "order_items": [{"name": "Crunchy", "quantity": 2.5, "unit": None}],
    }

    assert problem(empty, build_extraction_schema(pack.playbook, pack)) is None
