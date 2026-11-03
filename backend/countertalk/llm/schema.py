"""Checks an answer against the strict JSON schema it was asked for (batch 9).

Strict mode should make every answer match, but an answer is outside data (R4), and one that
doesn't match gets a repair call, which says what's wrong. Only the keywords strict mode allows
and our schemas use are known: any other keyword anywhere in the schema raises ValueError, so a
schema can't pass a check it was never given.
"""

import math
from collections.abc import Iterable
from typing import Any

# Keywords that say nothing about the value.
IGNORED = {"description", "$defs"}
CHECKED = {"type", "enum", "properties", "required", "additionalProperties", "items"}
CHECKED |= {"anyOf", "$ref"}
TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def problem(value: object, schema: dict[str, Any]) -> str | None:
    """What's wrong with `value`, as `path: what`, or None if it matches. An integer is a JSON
    number written without a fraction: 2.0 isn't one, so a caller's integers are Python ints."""
    check_schema(schema)
    return _problem(value, schema, schema.get("$defs", {}), "")


def check_schema(schema: dict[str, Any]) -> None:
    """Raises ValueError for anything in the schema this can't check, wherever it is: a client
    runs it before paying for an answer."""
    _check_part(schema, schema.get("$defs", {}) if isinstance(schema, dict) else {})


def _check_part(schema: object, defs: object) -> None:
    if not isinstance(schema, dict):
        raise ValueError("a schema must be an object")
    for keyword in schema:
        if keyword not in CHECKED | IGNORED:
            raise ValueError(f"can't check the schema keyword {keyword}")
    for name in _types(schema):
        if name not in TYPES:
            raise ValueError(f"unknown type {name}")
    if "$ref" in schema:
        ref = schema["$ref"]
        if (
            not isinstance(ref, str)
            or ref.removeprefix("#/$defs/") == ref
            or not (isinstance(defs, dict) and ref.removeprefix("#/$defs/") in defs)
        ):
            raise ValueError(f"the schema has no $defs entry for {ref}")
    if schema.get("additionalProperties", False) is not False:
        raise ValueError("additionalProperties must be false")
    if not isinstance(schema.get("anyOf", []), list):
        raise ValueError("anyOf must be a list of schemas")
    inner = [*schema.get("properties", {}).values(), *schema.get("$defs", {}).values()]
    inner += [*schema.get("anyOf", []), *([schema["items"]] if "items" in schema else [])]
    for part in inner:
        _check_part(part, defs)


def _types(schema: dict[str, Any]) -> list[str]:
    allowed = schema.get("type", [])
    return allowed if isinstance(allowed, list) else [allowed]


def _type_of(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _problem(value: object, schema: dict[str, Any], defs: dict[str, Any], path: str) -> str | None:
    at = f"{path}: " if path else ""
    if "$ref" in schema:  # checked by check_schema: one of the root's $defs
        return _problem(value, defs[schema["$ref"].removeprefix("#/$defs/")], defs, path)
    if "anyOf" in schema:
        if all(_problem(value, form, defs, path) for form in schema["anyOf"]):
            return f"{at}should match one of its forms"
        return None
    if "type" in schema:
        allowed = _types(schema)
        found = _type_of(value)
        # An integer is a number too, and a non-finite float is neither.
        fits = found in allowed or (found == "integer" and "number" in allowed)
        if not fits or (isinstance(value, float) and not math.isfinite(value)):
            return f"{at}should be {' or '.join(allowed)}, not {found}"
    # By type as well as value: in Python, True == 1.
    allowed_values = [(_type_of(item), item) for item in schema.get("enum", [])]
    if "enum" in schema and (_type_of(value), value) not in allowed_values:
        return f"{at}{value!r} isn't one of the allowed values"
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                return f"{at}{name} is missing"
        for name in value:
            if name not in properties:
                return f"{at}{name} isn't expected"
        for name, inner in properties.items():
            if name in value:
                found_problem = _problem(
                    value[name], inner, defs, f"{path}.{name}" if path else name
                )
                if found_problem:
                    return found_problem
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            found_problem = _problem(item, schema["items"], defs, f"{path}[{index}]")
            if found_problem:
                return found_problem
    return None


# --- building a strict schema (moved from playbooks, batch 17d: ask builds one too) --------


def nullable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A field that may be null. The one place this is written, so a switch to anyOf (if Muse
    Spark's strict mode refuses type lists, batch 9) is a one-line change."""
    nullable = {**schema, "type": [schema["type"], "null"]}
    if "enum" in schema:
        nullable["enum"] = [*schema["enum"], None]
    return nullable


def enum_schema(keys: Iterable[str]) -> dict[str, Any]:
    return {"type": "string", "enum": list(keys)}


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    # Strict mode: every key required, and nothing else allowed.
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def text_schema(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def list_schema(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}
