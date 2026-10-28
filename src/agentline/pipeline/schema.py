"""Validator for the JSON Schema subset agent output schemas use (stdlib only).

Supported: type (incl. lists of types), properties, required, additionalProperties (bool),
items, enum, minItems, maxItems, minLength. Unknown keywords are ignored.
"""

from __future__ import annotations

from typing import Any

_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _is(value: Any, type_name: str) -> bool:
    if type_name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES.get(type_name, (object,)))


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable errors (empty = valid)."""
    errors: list[str] = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is(value, x) for x in types):
            return [f"{path}: expected {' or '.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errors.append(f"{path}: shorter than {schema['minLength']} characters")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                errors.extend(validate(value[key], sub, f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}: unexpected property {key!r}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(items := schema.get("items"), dict):
            for i, item in enumerate(value):
                errors.extend(validate(item, items, f"{path}[{i}]"))
    return errors
