"""A minimal, safe writer for `[<section>.<name>]` tables in agentline.toml.

`set_table` replaces the body of one table (keeping its header line) or appends the table;
`delete_table` removes it. Everything else in the file stays byte-identical, comments
included. The result is parsed back with `tomllib` and must equal the old config with only
that table changed, otherwise a ValidationError is raised and nothing should be written.
Values: strings, numbers, booleans, lists and small inline tables (dicts).
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from agentline.errors import ValidationError

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")
_HEADER = re.compile(r"^\s*(\[\[?)(.*?)(\]\]?)\s*(#.*)?$")


def toml_key(key: str) -> str:
    return key if _BARE.match(key) else json.dumps(key)


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)  # JSON string escapes are valid TOML basic-string escapes
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        inner = ", ".join(f"{toml_key(str(k))} = {toml_value(x)}" for k, x in v.items())
        return "{ " + inner + " }" if inner else "{}"
    raise ValidationError(f"can't write {type(v).__name__} values to agentline.toml")


def table_body(table: dict[str, Any], comment: str | None = None) -> list[str]:
    lines = [f"# {comment}"] if comment else []
    for k, v in table.items():
        if v is None:
            continue
        lines.append(f"{toml_key(k)} = {toml_value(v)}")
    return lines


def _parse_dotted(text: str) -> list[str] | None:
    keys: list[str] = []
    i, n = 0, len(text)
    while True:
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            return None
        if text[i] == '"':
            m = re.match(r'"((?:[^"\\]|\\.)*)"', text[i:])
            if not m:
                return None
            keys.append(json.loads(m.group(0)))
            i += m.end()
        elif text[i] == "'":
            j = text.find("'", i + 1)
            if j < 0:
                return None
            keys.append(text[i + 1 : j])
            i = j + 1
        else:
            m = re.match(r"[A-Za-z0-9_-]+", text[i:])
            if not m:
                return None
            keys.append(m.group(0))
            i += m.end()
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            return keys
        if text[i] != ".":
            return None
        i += 1


def header_keys(line: str) -> tuple[bool, list[str]] | None:
    """(is_array_table, dotted keys) if the line is a table header, else None."""
    m = _HEADER.match(line)
    if not m or len(m.group(1)) != len(m.group(3)):
        return None
    keys = _parse_dotted(m.group(2))
    return None if keys is None else (len(m.group(1)) == 2, keys)


def _span(lines: list[str], section: str, name: str) -> tuple[int, int, int] | None:
    """(header index, end of body without trailing blanks/comments, next header index)."""
    start = None
    for i, line in enumerate(lines):
        h = header_keys(line)
        if h and not h[0] and h[1] == [section, name]:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if header_keys(lines[j]):
            end = j
            break
    k = end
    while k > start + 1 and (not lines[k - 1].strip() or lines[k - 1].lstrip()[:1] == "#"):
        k -= 1
    return start, k, end


def _without(raw: dict[str, Any], section: str, name: str) -> dict[str, Any]:
    out = json.loads(json.dumps(raw, default=str))
    sec = out.get(section)
    if isinstance(sec, dict):
        sec.pop(name, None)
        if not sec:
            out.pop(section)
    return out


def _verify(old: str, new: str, section: str, name: str, table: dict[str, Any] | None) -> None:
    try:
        before, after = tomllib.loads(old), tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        msg = f"can't rewrite agentline.toml safely ({e}); edit it by hand"
        raise ValidationError(msg) from None
    got = after.get(section, {}).get(name)
    if table is None:
        if got is not None:
            raise ValidationError(
                f"can't remove [{section}.{name}] safely (is it defined inline or with dotted"
                " keys?); edit agentline.toml by hand"
            )
    else:
        expect = {k: v for k, v in table.items() if v is not None}
        own = (
            {k: v for k, v in got.items() if not isinstance(v, dict)}
            if isinstance(got, dict)
            else None
        )
        if own is None or own != {k: v for k, v in expect.items() if not isinstance(v, dict)}:
            raise ValidationError(
                f"can't rewrite [{section}.{name}] safely (is it defined inline, with dotted"
                " keys or with sub-tables?); edit agentline.toml by hand"
            )
    if _without(before, section, name) != _without(after, section, name):
        raise ValidationError("rewriting agentline.toml would change other settings; aborted")


# Sections renamed since: a write to the new one first renames the old headers in place.
RENAMED = {"harnesses": "agents"}


def migrate_section(text: str, section: str) -> str:
    """`text` with `[<old>.<name>]` headers of `section`'s old name renamed to `[section.name]`
    (comments and everything else kept). Unchanged when nothing uses the old name."""
    old = RENAMED.get(section)
    if old is None:
        return text
    lines = text.splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        h = header_keys(line)
        if h and not h[0] and len(h[1]) == 2 and h[1][0] == old:
            m = _HEADER.match(line)
            assert m is not None
            head = f"[{toml_key(section)}.{toml_key(h[1][1])}]"
            lines[i] = line[: m.start(1)] + head + line[m.end(3) :]
            changed = True
    if not changed:
        return text
    new = "".join(lines)
    try:
        before, after = tomllib.loads(text), tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        msg = f"can't rewrite agentline.toml safely ({e}); edit it by hand"
        raise ValidationError(msg) from None
    moved = {**before.pop(old, {}), **before.pop(section, {})}
    if after.pop(section, None) != moved or after.pop(old, None) is not None or before != after:
        raise ValidationError(
            f"can't rename [{old}.*] to [{section}.*] safely (defined inline or with dotted"
            " keys?); edit agentline.toml by hand"
        )
    return new


def set_table(
    text: str, section: str, name: str, table: dict[str, Any], comment: str | None = None
) -> str:
    """`text` with `[section.name]`'s body replaced by `table`, or the table appended."""
    text = migrate_section(text, section)
    lines = text.splitlines(keepends=True)
    body = [x + "\n" for x in table_body(table, comment)]
    span = _span(lines, section, name)
    if span is None:
        prefix = text if not text or text.endswith("\n") else text + "\n"
        sep = "\n" if prefix.strip() else ""
        new = prefix + sep + f"[{toml_key(section)}.{toml_key(name)}]\n" + "".join(body)
    else:
        start, k, _ = span
        head = "".join(lines[: start + 1])
        if not head.endswith("\n"):
            head += "\n"
        new = head + "".join(body) + "".join(lines[k:])
    _verify(text, new, section, name, table)
    return new


def delete_table(text: str, section: str, name: str) -> str:
    """`text` without `[section.name]` (header, body and the blank lines after it)."""
    text = migrate_section(text, section)
    lines = text.splitlines(keepends=True)
    span = _span(lines, section, name)
    if span is None:
        return text
    start, _, end = span
    # drop the table's own trailing blank lines too, but keep comments that precede the next one
    k = end
    while k > start + 1 and lines[k - 1].lstrip()[:1] == "#":
        k -= 1
    new = "".join(lines[:start]) + "".join(lines[k:])
    new = re.sub(r"\n{3,}", "\n\n", new)
    _verify(text, new, section, name, None)
    return new


def write_file(path: Path, text: str) -> None:
    """Atomic replace, keeping a `.bak` of the previous version."""
    if path.exists():
        path.with_name(path.name + ".bak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def set_plain_table(text: str, section: str, table: dict[str, Any]) -> str:
    """`text` with a top-level `[section]` (e.g. `[report]`) replaced by `table` (dropped when
    empty), or appended. Refuses when anything else would change."""
    lines = text.splitlines(keepends=True)
    start = next(
        (
            i
            for i, ln in enumerate(lines)
            if (h := header_keys(ln)) and not h[0] and h[1] == [section]
        ),
        None,
    )
    clean = {k: v for k, v in table.items() if v is not None}
    body = [x + "\n" for x in table_body(clean)] if clean else []
    if start is None:
        if not body:
            return text
        prefix = text if not text or text.endswith("\n") else text + "\n"
        new = prefix + ("\n" if prefix.strip() else "") + f"[{toml_key(section)}]\n" + "".join(body)
    else:
        end = next((j for j in range(start + 1, len(lines)) if header_keys(lines[j])), len(lines))
        k = end
        while k > start + 1 and (not lines[k - 1].strip() or lines[k - 1].lstrip()[:1] == "#"):
            k -= 1
        head = lines[: start + 1] if body else lines[:start]
        new = "".join(head) + "".join(body) + "".join(lines[k:])
        new = re.sub(r"\n{3,}", "\n\n", new)
    try:
        before, after = tomllib.loads(text), tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        raise ValidationError(
            f"can't rewrite agentline.toml safely ({e}); edit it by hand"
        ) from None
    got = after.pop(section, {})
    before.pop(section, None)
    if before != after or got != clean:
        raise ValidationError(f"can't rewrite [{section}] safely; edit agentline.toml by hand")
    return new
