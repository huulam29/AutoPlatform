"""Deliberately tiny prompt templating: `{{a.b.c}}` and `{{#name?}}...{{/name?}}` blocks.

No logic, no loops, no filters. Dicts and lists render as indented JSON. A block renders only
when its value is present and truthy. References are checked against a node's declared inputs
by the validator, using `references()`.

A large input arrives as an artifact reference (`{"$artifact": hash, "summary", "path", ...}`).
`{{plan}}` renders it as its summary plus `(full content: <path>)`; `{{plan.field}}` reads the
field from the referenced file, so field access works whatever the input's size.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agentline.artifacts import ArtifactRef, is_ref, render_ref

_VAR = re.compile(r"\{\{\s*([A-Za-z_][\w.]*\??)\s*\}\}")
_BLOCK = re.compile(r"\{\{#\s*([A-Za-z_][\w.]*\??)\s*\}\}(.*?)\{\{/\s*\1\s*\}\}", re.DOTALL)
_ANY = re.compile(r"\{\{[#/]?\s*([A-Za-z_][\w.]*\??)\s*\}\}")

MISSING = object()


class TemplateError(Exception):
    pass


def lookup(context: dict[str, Any], path: str) -> Any:
    cur: Any = context
    for part in path.rstrip("?").split("."):
        if is_ref(cur):
            try:
                cur = ArtifactRef.from_value(cur).read_file()
            except (OSError, ValueError):
                return MISSING
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return MISSING
    return cur


def _fmt(value: Any) -> str:
    if isinstance(value, str):
        return value
    if is_ref(value):
        return render_ref(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)
    return "" if value is None else str(value)


def render(template: str, context: dict[str, Any]) -> str:
    def block(m: re.Match[str]) -> str:
        value = lookup(context, m.group(1))
        return render(m.group(2), context) if value is not MISSING and value else ""

    out = _BLOCK.sub(block, template)

    def var(m: re.Match[str]) -> str:
        name = m.group(1)
        value = lookup(context, name)
        if value is MISSING:
            if name.endswith("?"):
                return ""
            raise TemplateError(f"template variable {name!r} is not available")
        return _fmt(value)

    return _VAR.sub(var, out)


def references(template: str) -> set[str]:
    """Root names a template refers to, e.g. {'ticket', 'review'}."""
    return {m.group(1).rstrip("?").split(".")[0] for m in _ANY.finditer(template)}


_GUARD = re.compile(r"\{\{#\s*([A-Za-z_][\w.]*)\?\s*\}\}[\s\S]*?\{\{/\s*\1\?\s*\}\}")


def unguarded_references(template: str) -> set[str]:
    """Root names used outside `{{#name?}}...{{/name?}}` blocks (and not as `{{name?}}`):
    for an optional input these fail to render when it's missing (the first pass of a loop)."""
    outside = _GUARD.sub("", template)
    return {
        m.group(1).split(".")[0] for m in _ANY.finditer(outside) if not m.group(1).endswith("?")
    }


def render_value(value: Any, context: dict[str, Any]) -> Any:
    """Render templates inside a `with:` structure (strings only; other types kept)."""
    if isinstance(value, str):
        return render(value, context)
    if isinstance(value, dict):
        return {k: render_value(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, context) for v in value]
    return value


def value_references(value: Any) -> set[str]:
    if isinstance(value, str):
        return references(value)
    if isinstance(value, dict):
        return set().union(*(value_references(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(value_references(v) for v in value)) if value else set()
    return set()
