"""Comment-preserving round trip between pipeline YAML and the editor's JSON graph model.

`to_model(text)` reads the YAML into a plain JSON model (`nodes`, `edges`, `issues`, ...).
`apply_model(text, model)` writes a model back by editing the ruamel round-trip tree in place:
only fields whose values changed are touched, new nodes go to the end of `nodes`, removed nodes
are deleted, node order is kept, and untouched edge chains (`a -> b -> c`) stay as they are.

ruamel re-emits some things in its own style (`{ a: 1 }` becomes `{a: 1}`), so the edited tree
is not written out directly. Instead we dump the tree twice, before and after the edit, and
replay only the lines that differ onto the original text (`rebase`). Unchanged lines keep their
exact bytes; a changed line that ruamel restyled gets the original's spacing back at character
level. Comments that exist in the original but not in the result are reported as warnings.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.compat import ordereddict
from ruamel.yaml.error import CommentMark, YAMLError
from ruamel.yaml.scalarstring import LiteralScalarString
from ruamel.yaml.tokens import CommentToken

from agentline.pipeline.spec import Issue, SpecError, parse_spec

TOP_MODEL_FIELDS = (
    "name",
    "version",
    "description",
    "trigger",
    "start",
    "budget_usd",
    "defaults",
)
EDGE_ATTRS = ("when", "max_loops", "on_exhausted")
_EDGE_ITEM = re.compile(r"^edges\[(\d+)\]")
_NODE_PATH = re.compile(r"^nodes\.([^.\[]+)(?:\.([^.\[]+))?")

Indent = tuple[int, int, int]  # mapping, sequence, offset


# -- loading and dumping ----------------------------------------------------------------------
def _yaml(indent: Indent = (2, 4, 2)) -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=indent[0], sequence=indent[1], offset=indent[2])
    return y


def load_document(text: str) -> Any:
    """The ruamel round-trip document (a CommentedMap for a valid pipeline). Raises YAMLError."""
    return _yaml().load(text)


def _dump(doc: Any, indent: Indent) -> str:
    buf = io.StringIO()
    _yaml(indent).dump(doc, buf)
    return buf.getvalue()


def detect_indent(doc: Any) -> Indent:
    """Mapping indent from the first nested mapping; sequence offset from the edges list."""
    mapping, offset = 2, 2
    if isinstance(doc, CommentedMap):
        nodes = doc.get("nodes")
        if isinstance(nodes, CommentedMap) and nodes and not nodes.fa.flow_style():
            try:
                col = nodes.lc.key(next(iter(nodes)))[1]
                mapping = col if col > 0 else 2
            except (KeyError, TypeError, StopIteration):
                pass
        edges = doc.get("edges")
        if isinstance(edges, CommentedSeq) and edges and not edges.fa.flow_style():
            with contextlib.suppress(KeyError, TypeError, IndexError):
                offset = edges.lc.item(0)[1] - 2
    offset = max(offset, 0)
    return (mapping, offset + 2, offset)


# -- plain values -----------------------------------------------------------------------------
def plain(value: Any) -> Any:
    """ruamel values -> plain JSON-able Python values."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return str(value)  # dates and other YAML scalars


def same(a: Any, b: Any) -> bool:
    """JSON-ish equality: 5.0 == 5 (JS drops the .0), but True != 1."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


# -- model ------------------------------------------------------------------------------------
def _edge_items(edges: Any) -> list[tuple[int, list[dict[str, Any]]]]:
    """Per YAML edges item: the model edges it defines (the same rules as `spec._edges`)."""
    out: list[tuple[int, list[dict[str, Any]]]] = []
    if not isinstance(edges, list):
        return out
    for i, raw in enumerate(edges):
        items: list[dict[str, Any]] = []
        if isinstance(raw, str):
            parts = [x.strip() for x in raw.split("->")]
            ok = len(parts) >= 2 and all(parts) and not any("[" in x or "]" in x for x in parts)
            if ok:
                items = [
                    {"src": a, "dst": b, "chain_index": i} for a, b in itertools.pairwise(parts)
                ]
        elif isinstance(raw, dict):
            src, dst = raw.get("from"), raw.get("to")
            if isinstance(src, str) and isinstance(dst, str):
                e: dict[str, Any] = {"src": src, "dst": dst}
                for k in EDGE_ATTRS:
                    if k in raw:
                        e[k] = plain(raw[k])
                e["chain_index"] = i
                items = [e]
        out.append((i, items))
    return out


RENAMED_KEYS = {"agent": "harness"}  # agent-node and `defaults` keys renamed in phase 35


def _rename_key(m: CommentedMap, old: str, new: str) -> None:
    """`old` renamed to `new` at the same position, its comments kept (a trailing comment
    moves right by the length difference, so its gap to the value stays the same)."""
    pos = list(m).index(old)
    value = m[old]
    comment = m.ca.items.pop(old, None)
    del m[old]
    m.insert(pos, new, value)
    if comment is not None:
        for tok in comment:
            if isinstance(tok, CommentToken) and tok.column:
                tok.column += len(new) - len(old)
        m.ca.items[new] = comment


def migrate_doc(doc: Any) -> None:
    """Rename old keys in place: `agent:` -> `harness:` on agent nodes and in `defaults`, so
    the model the editor gets only has the new names. A map that already has the new key is
    left alone (the validator reports both)."""
    if not isinstance(doc, CommentedMap):
        return
    maps = [doc.get("defaults")]
    nodes = doc.get("nodes")
    if isinstance(nodes, dict):
        maps += [
            n for n in nodes.values() if isinstance(n, dict) and n.get("type") in ("agent", "chat")
        ]
    for m in maps:
        if isinstance(m, CommentedMap):
            for old, new in RENAMED_KEYS.items():
                if old in m and new not in m:
                    _rename_key(m, old, new)


def _respell(m: Any, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """The editor's `new` fields (new key names, see `migrate_doc`) against the file's `old`
    ones: an unchanged renamed value goes back to the file's spelling (the file stays
    byte-identical); a changed one renames the key in the file first (`old` follows)."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new
    for o, n in RENAMED_KEYS.items():
        if o in old and n not in old and n in new and o not in new:
            if same(new[n], old[o]):
                new = {(o if k == n else k): v for k, v in new.items()}
            elif isinstance(m, CommentedMap) and o in m:
                _rename_key(m, o, n)
                old[n] = old.pop(o)
    return new


def _model_from_doc(doc: Any) -> dict[str, Any]:
    model: dict[str, Any] = {
        "name": None,
        "version": None,
        "description": None,
        "trigger": None,
        "start": None,
        "budget_usd": None,
        "defaults": None,
        "nodes": [],
        "edges": [],
    }
    if not isinstance(doc, dict):
        return model
    for k in TOP_MODEL_FIELDS:
        if k in doc:
            model[k] = plain(doc[k])
    nodes = doc.get("nodes")
    if isinstance(nodes, dict):
        for node_id, raw in nodes.items():
            fields = plain(raw) if isinstance(raw, dict) else {}
            model["nodes"].append(
                {"id": str(node_id), "type": fields.get("type"), "fields": fields}
            )
    for _, items in _edge_items(doc.get("edges")):
        model["edges"].extend(items)
    return model


def _issue_dict(
    issue: Issue, model: dict[str, Any], *, parse_phase: bool, line_nodes: dict[int, str]
) -> dict[str, Any]:
    d = issue.as_dict()
    d["node_id"] = None
    d["field"] = None
    d["edge_index"] = None
    d["edge_indexes"] = []
    if m := _NODE_PATH.match(issue.path):
        if any(n["id"] == m.group(1) for n in model["nodes"]):
            d["node_id"] = m.group(1)
            d["field"] = m.group(2)
    elif m := _EDGE_ITEM.match(issue.path):
        i = int(m.group(1))
        if parse_phase:  # the parser counts YAML items; the validator counts expanded edges
            d["edge_indexes"] = [
                k for k, e in enumerate(model["edges"]) if e.get("chain_index") == i
            ]
        elif 0 <= i < len(model["edges"]):
            d["edge_indexes"] = [i]
        d["edge_index"] = d["edge_indexes"][0] if d["edge_indexes"] else None
    elif issue.path == "edges" and issue.line in line_nodes:  # e.g. a cycle, at its first node
        d["node_id"] = line_nodes[issue.line]
    return d


def validate_text(
    text: str,
    model: dict[str, Any],
    *,
    path: Path | None = None,
    base_dir: Path | None = None,
    registry: Any = None,
    config: Any = None,
) -> list[dict[str, Any]]:
    """Issues from `parse_spec` + `validate_spec`, mapped to `node_id` / `edge_indexes`."""
    from agentline.pipeline.validate import validate_spec

    if registry is None:
        from agentline.builtins import default_registry

        registry = default_registry(config)
    try:
        spec = parse_spec(text, path=path, base_dir=base_dir)
    except SpecError as e:
        issues, parse_phase, line_nodes = e.issues, True, {}
    else:
        issues = validate_spec(spec, registry, config)
        parse_phase = False
        line_nodes = {n.line: n.id for n in spec.nodes.values() if n.line}
    return [_issue_dict(i, model, parse_phase=parse_phase, line_nodes=line_nodes) for i in issues]


def to_model(
    text: str,
    *,
    path: Path | None = None,
    base_dir: Path | None = None,
    registry: Any = None,
    config: Any = None,
    validate: bool = True,
) -> dict[str, Any]:
    """JSON graph model of a pipeline: `{name, version, description, trigger, budget_usd,
    nodes: [{id, type, fields}], edges: [{src, dst, when?, max_loops?, on_exhausted?,
    chain_index}], issues: [...]}`. Never raises: broken YAML gives an empty graph plus issues."""
    try:
        doc = load_document(text)
    except YAMLError:
        doc = None
    migrate_doc(doc)
    model = _model_from_doc(doc)
    model["issues"] = (
        validate_text(text, model, path=path, base_dir=base_dir, registry=registry, config=config)
        if validate
        else []
    )
    return model


# -- comments ---------------------------------------------------------------------------------
def _tokens(obj: Any) -> list[CommentToken]:
    if isinstance(obj, CommentToken):
        return [obj]
    if isinstance(obj, (list, tuple)):
        return [t for x in obj for t in _tokens(x)]
    return []


def _comments(doc: Any) -> Counter[str]:
    """Every `# ...` comment line in the document (blank-line tokens don't count)."""
    seen: set[int] = set()
    found: Counter[str] = Counter()

    def walk(node: Any) -> None:
        if isinstance(node, (CommentedMap, CommentedSeq)):
            ca = node.ca
            toks = _tokens(ca.comment) + _tokens(ca.end) + _tokens(list(ca.items.values()))
            for t in toks:
                if id(t) in seen:
                    continue
                seen.add(id(t))
                for line in str(t.value).splitlines():
                    if line.strip().startswith("#"):
                        found[line.strip()] += 1
            children = node.values() if isinstance(node, CommentedMap) else node
            for child in children:
                walk(child)

    walk(doc)
    return found


def _token(value: str, column: int = 0) -> CommentToken:
    return CommentToken(value, CommentMark(column))


def _post_index(container: Any) -> int:
    return 2 if isinstance(container, CommentedMap) else 0


def _get_post(container: Any, key: Any) -> CommentToken | None:
    slot = container.ca.items.get(key)
    if not slot:
        return None
    tok = slot[_post_index(container)]
    return tok if isinstance(tok, CommentToken) else None


def _set_post(container: Any, key: Any, tok: CommentToken | None) -> None:
    size = 4 if isinstance(container, CommentedMap) else 2
    slot = container.ca.items.setdefault(key, [None] * size)
    while len(slot) < size:
        slot.append(None)
    slot[_post_index(container)] = tok
    value = container[key]
    if (
        isinstance(value, (CommentedMap, CommentedSeq))
        and value.fa.flow_style()
        and isinstance(value.ca.comment, list)
        and value.ca.comment
    ):
        value.ca.comment[0] = tok  # ruamel mirrors a flow value's trailing comment here


def _entry_slot(container: Any, key: Any) -> tuple[Any, Any]:
    """Where ruamel keeps the comment and blank lines that follow `container[key]`: on the
    key itself, or for a block collection value on its innermost last entry."""
    value = container[key]
    if (
        isinstance(value, (CommentedMap, CommentedSeq))
        and value
        and not value.fa.flow_style()
        and (inner := _last_slot(value)) is not None
    ):
        return inner
    return (container, key)


def _last_slot(container: Any) -> tuple[Any, Any] | None:
    """The slot holding what follows a block collection (None for empty or flow ones)."""
    if not isinstance(container, (CommentedMap, CommentedSeq)) or not container:
        return None
    if container.fa.flow_style():
        return None
    key = next(reversed(container)) if isinstance(container, CommentedMap) else len(container) - 1
    return _entry_slot(container, key)


def _is_block_scalar(slot: tuple[Any, Any]) -> bool:
    return isinstance(slot[0][slot[1]], LiteralScalarString)


def _split_trailing(tok: CommentToken, block_scalar: bool = False) -> tuple[str, str]:
    """('  # end of line\n', '\n# lines after\n') from a post comment. After a `|` block
    scalar the token has no end-of-line part: the scalar ends its own last line."""
    value = str(tok.value)
    if block_scalar:
        return "", value
    first, sep, rest = value.partition("\n")
    return first + sep, rest


def _detach(slot: tuple[Any, Any] | None) -> str:
    """Remove and return the lines after a slot's end of line (blank lines, comments)."""
    if slot is None:
        return ""
    tok = _get_post(*slot)
    if tok is None:
        return ""
    eol, rest = _split_trailing(tok, _is_block_scalar(slot))
    if not rest:
        return ""
    if eol.strip():
        tok.value = eol
    else:
        _set_post(*slot, None)
    return rest


def _attach(slot: tuple[Any, Any] | None, rest: str, *, merge: bool = False) -> None:
    """Put `rest` after a slot's line. With `merge`, lines already there win and only extra
    comment lines are added (deleting a node must not double the blank line)."""
    if not rest or slot is None:
        return
    block = _is_block_scalar(slot)
    tok = _get_post(*slot)
    if tok is None:
        _set_post(*slot, _token(rest if block else "\n" + rest))
        return
    eol, existing = _split_trailing(tok, block)
    if merge and existing:
        extra = [x for x in rest.splitlines(keepends=True) if x.strip()]
        if not extra:
            return
        rest = "".join(extra)
    if not eol and not block:
        eol = "\n"
    tok.value = eol + existing + rest


def _detach_trailing(container: Any) -> str:
    return _detach(_last_slot(container))


def _attach_trailing(container: Any, rest: str, *, merge: bool = False) -> None:
    _attach(_last_slot(container), rest, merge=merge)


def _restyle_post(container: Any, key: Any, old: Any, new: Any) -> None:
    """A plain scalar's post comment starts with its end of line; a block scalar's doesn't."""
    was, now = isinstance(old, LiteralScalarString), isinstance(new, LiteralScalarString)
    tok = _get_post(container, key)
    if tok is None or was == now:
        return
    value = str(tok.value)
    if now and value.startswith("\n"):
        tok.value = value[1:]
        if not tok.value:
            _set_post(container, key, None)
    elif was:
        tok.value = "\n" + value


# -- building YAML values ---------------------------------------------------------------------
def _scalar_like(new: Any, old: Any) -> Any:
    if isinstance(new, str):
        if "\n" in new:
            return LiteralScalarString(new)
        if isinstance(old, LiteralScalarString):
            return new
        if isinstance(old, str) and type(old) is not str:
            try:
                return type(old)(new)  # keep the quote style ("..." / '...')
            except TypeError:
                return new
    return new


def build(value: Any, *, flow: bool | None = None) -> Any:
    """Plain value -> ruamel value in the style the example uses: short scalar lists and small
    scalar maps inline (`[a, b]`, `{ env: eod }`), everything else block style."""
    if isinstance(value, dict):
        m = CommentedMap()
        for k, v in value.items():
            m[k] = build(v)
        scalars = all(not isinstance(v, (dict, list)) for v in value.values())
        if flow if flow is not None else scalars and len(json.dumps(value)) <= 60:
            m.fa.set_flow_style()
        else:
            m.fa.set_block_style()
        return m
    if isinstance(value, list):
        s = CommentedSeq(build(v) for v in value)
        scalars = all(not isinstance(v, (dict, list)) for v in value)
        if flow if flow is not None else scalars and len(json.dumps(value)) <= 80:
            s.fa.set_flow_style()
        else:
            s.fa.set_block_style()
        return s
    if isinstance(value, str) and "\n" in value:
        return LiteralScalarString(value)
    return value


def _set_key(m: CommentedMap, key: str, value: Any) -> None:
    """Set a new key at the end of `m`, keeping the lines that followed `m` after it."""
    rest = _detach_trailing(m)
    m[key] = value
    _attach_trailing(m, rest)


def _del_key(m: CommentedMap, key: Any) -> None:
    """Delete `key`; the blank lines and comments after its entry move to the entry before
    it (they usually introduce the next key, not the deleted one)."""
    keys = list(m)
    pos = keys.index(key)
    rest = _detach(_entry_slot(m, key))
    del m[key]
    m.ca.items.pop(key, None)
    if pos > 0:
        _attach(_entry_slot(m, keys[pos - 1]), rest, merge=True)


def update_value(container: Any, key: Any, new: Any) -> None:
    """Make `container[key]` equal `new`, editing nested maps and lists in place so their
    style (flow/block, quoting) and comments survive."""
    old = container[key]
    if same(plain(old), new):
        return
    if new in ({}, []):  # an empty block collection can't be written; `[]` / `{}` can
        rest = _detach(_entry_slot(container, key))
        container[key] = build(new, flow=True)
        _attach(_entry_slot(container, key), rest)
        return
    if isinstance(old, CommentedMap) and isinstance(new, dict):
        for k in [k for k in old if str(k) not in new]:
            _del_key(old, k)
        for k, v in new.items():
            if k in old:
                update_value(old, k, v)
            elif old.fa.flow_style():
                old[k] = build(v, flow=True)
            else:
                _set_key(old, k, build(v))
        return
    if isinstance(old, CommentedSeq) and isinstance(new, list):
        for i, v in enumerate(new[: len(old)]):
            update_value(old, i, v)
        if len(new) < len(old):
            rest = _detach_trailing(old) if not old.fa.flow_style() else ""
            del old[len(new) :]
            for i in [i for i in old.ca.items if i >= len(new)]:
                del old.ca.items[i]
            _attach_trailing(old, rest, merge=True)
        for v in new[len(old) :]:
            if old.fa.flow_style():
                old.append(build(v, flow=True))
            else:
                rest = _detach_trailing(old)
                old.append(build(v))
                _attach_trailing(old, rest)
        return
    if isinstance(new, (dict, list)):
        built = build(new)
        if isinstance(old, (CommentedMap, CommentedSeq)) and old.fa.flow_style():
            built = build(new, flow=True)
        container[key] = built
        return
    value = _scalar_like(new, old)
    if isinstance(container, CommentedMap) and isinstance(old, LiteralScalarString):
        ordereddict.__setitem__(container, key, value)  # CommentedMap would keep the `|`
    else:
        container[key] = value
    _restyle_post(container, key, old, value)


def _update_map(m: CommentedMap, old: dict[str, Any], new: dict[str, Any]) -> None:
    """Apply field changes `old` -> `new` to the YAML map `m` (whose plain value is `old`)."""
    for k in [k for k in old if k not in new]:
        if k in m:
            _del_key(m, k)
    for k, v in new.items():
        if k in m:
            update_value(m, k, v)
        elif m.fa.flow_style():
            m[k] = build(v, flow=True)
        else:
            _set_key(m, k, build(v))


# -- edges ------------------------------------------------------------------------------------
def _edge_key(e: dict[str, Any]) -> tuple[Any, ...]:
    return (
        e.get("src"),
        e.get("dst"),
        e.get("when"),
        e.get("max_loops"),
        e.get("on_exhausted"),
    )


def _plain_edge(e: dict[str, Any]) -> bool:
    return all(e.get(k) is None for k in EDGE_ATTRS)


def _edge_map(e: dict[str, Any]) -> CommentedMap:
    m = CommentedMap()
    m["from"] = e["src"]
    m["to"] = e["dst"]
    for k in EDGE_ATTRS:
        if e.get(k) is not None:
            m[k] = e[k]
    m.fa.set_flow_style()
    return m


def _chains(edges: Sequence[dict[str, Any]]) -> list[Any]:
    """Edges -> YAML items: consecutive plain edges join into `a -> b -> c` chains."""
    out: list[Any] = []
    chain: list[str] = []
    for e in edges:
        if not _plain_edge(e):
            if chain:
                out.append(" -> ".join(chain))
                chain = []
            out.append(_edge_map(e))
            continue
        if chain and chain[-1] == e["src"]:
            chain.append(e["dst"])
        else:
            if chain:
                out.append(" -> ".join(chain))
            chain = [e["src"], e["dst"]]
    if chain:
        out.append(" -> ".join(chain))
    return out


def _take(pool: list[dict[str, Any]], key: tuple[Any, ...]) -> dict[str, Any] | None:
    for i, e in enumerate(pool):
        if _edge_key(e) == key:
            return pool.pop(i)
    return None


def _path(
    pool: list[dict[str, Any]], u: str, v: str, depth: int = 4
) -> list[dict[str, Any]] | None:
    """Shortest path of plain (unconditional) edges u -> ... -> v in `pool`."""
    frontier: list[tuple[str, list[dict[str, Any]]]] = [(u, [])]
    seen = {u}
    for _ in range(depth):
        nxt: list[tuple[str, list[dict[str, Any]]]] = []
        for node, path in frontier:
            for e in pool:
                if e["src"] != node or not _plain_edge(e):
                    continue
                if e["dst"] == v:
                    return [*path, e]
                if e["dst"] not in seen:
                    seen.add(e["dst"])
                    nxt.append((e["dst"], [*path, e]))
        frontier = nxt
    return None


def _rechain(
    group: list[dict[str, Any]], kept: list[bool], pool: list[dict[str, Any]]
) -> list[list[str]]:
    """Node lists for a chain that lost edges. Gaps are bridged by new plain edges from the
    pool, so inserting `x` between `a` and `b` gives `a -> x -> b` in the same chain."""
    nodes = [group[0]["src"]] + [e["dst"] for e in group]
    pieces: list[list[str]] = [[nodes[0]]]
    for i, ok in enumerate(kept):
        if ok:
            pieces[-1].append(nodes[i + 1])
        else:
            pieces.append([nodes[i + 1]])
    # a lone node that no new edge touches was deleted or renamed: bridge over it
    alive = {x for e in pool for x in (e["src"], e["dst"])} | {
        x for e, ok in zip(group, kept, strict=True) if ok for x in (e["src"], e["dst"])
    }
    pieces = [p for p in pieces if len(p) > 1 or p[0] in alive] or [[nodes[0]]]
    merged = [pieces[0]]
    for piece in pieces[1:]:
        path = _path(pool, merged[-1][-1], piece[0])
        if path is None:
            merged.append(piece)
            continue
        for e in path:
            pool.remove(e)
        merged[-1] = merged[-1] + [e["dst"] for e in path] + piece[1:]
    return [p for p in merged if len(p) > 1]


def _apply_edges(
    doc: CommentedMap, old: list[dict[str, Any]], new: list[dict[str, Any]], offset: int
) -> None:
    if Counter(map(_edge_key, old)) == Counter(map(_edge_key, new)):
        return
    seq = doc.get("edges")
    if not isinstance(seq, CommentedSeq):
        if not new:
            return
        seq = CommentedSeq()
        if "edges" in doc:
            doc["edges"] = seq
        else:
            _set_key(doc, "edges", seq)
    pool = [dict(e) for e in new]
    groups = dict(_edge_items(seq))
    # pass 1: items whose edges all survive are kept verbatim
    kept: dict[int, list[bool]] = {}
    for i, group in groups.items():
        kept[i] = [_take(pool, _edge_key(e)) is not None for e in group]
    # pass 2: edit changed map items in place, re-chain broken chains
    plan: list[tuple[int | None, Any]] = []  # (old item index or None, item)
    for i, item in enumerate(seq):
        group = groups.get(i, [])
        if not group or all(kept[i]):  # unchanged (or rejected by the parser: user fixes it)
            plan.append((i, item))
            continue
        if isinstance(item, dict):
            edited = next((x for x in pool if x.get("chain_index") == i), None)
            if edited is not None:
                pool.remove(edited)
                _update_map(
                    item,
                    plain(item),
                    {
                        "from": edited["src"],
                        "to": edited["dst"],
                        **{k: edited[k] for k in EDGE_ATTRS if edited.get(k) is not None},
                    },
                )
                plan.append((i, item))
            continue
        # the chain lost an edge: re-emit what's left; the first piece keeps its comments
        for k, nodes in enumerate(_rechain(group, kept[i], pool)):
            plan.append((i if k == 0 else None, " -> ".join(nodes)))
    # pass 3: new edges at the end
    plan.extend((None, x) for x in _chains(pool))

    # rebuild the sequence in place, carrying comments with the items they belong to
    old_ca = dict(seq.ca.items)
    last_old = len(seq) - 1
    tail = ""
    if last_old >= 0 and (tok := _get_post(seq, last_old)) is not None:
        eol, tail = _split_trailing(tok)
        tok.value = eol
    if not plan:
        update_value(doc, "edges", [])
        return
    originals = list(seq)
    seq.ca.items.clear()
    del seq[:]
    used: set[int] = set()
    for old_i, item in plan:
        seq.append(item)
        if old_i is None or old_i in used or old_i not in old_ca:
            continue
        used.add(old_i)
        slot = list(old_ca[old_i])
        tok = slot[0]
        orig = originals[old_i]
        if isinstance(tok, CommentToken) and item is not orig and isinstance(orig, str):
            # an end-of-line comment keeps its distance from the (now shorter) chain text
            gap = max(tok.column - (offset + 2 + len(orig)), 1)
            slot[0] = _token(str(tok.value), offset + 2 + len(str(item)) + gap)
        seq.ca.items[len(seq) - 1] = slot
    if seq:
        slot = len(seq) - 1
        tok = _get_post(seq, slot)
        if tail:
            if tok is None:
                seq.ca.items[slot] = [_token("\n" + tail), None]
            else:
                tok.value = str(tok.value) + tail


# -- apply ------------------------------------------------------------------------------------
def apply_model(text: str, model: dict[str, Any]) -> tuple[str, list[str]]:
    """Write `model` into `text` with minimal edits. Returns `(new_text, warnings)`.
    Raises ValueError when `text` isn't a YAML mapping (fix the source first)."""
    try:
        doc = load_document(text)
    except YAMLError as e:
        raise ValueError(f"the pipeline file is not valid YAML: {e}") from None
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise ValueError("the pipeline file must be a YAML mapping")
    indent = detect_indent(doc)
    before = _dump(doc, indent)
    old = _model_from_doc(doc)
    warnings: list[str] = []

    # top-level fields
    for k in TOP_MODEL_FIELDS:
        if k not in model:
            continue
        value = model[k]
        if k in ("defaults", "start") and value == {}:
            value = None  # nothing left: drop the section instead of writing `{}`
        if k == "defaults":
            value = _respell(doc.get("defaults"), old[k], value)
        if same(old[k], value):
            continue
        if value is None:
            if k in doc:
                _del_key(doc, k)
        elif k in doc:
            update_value(doc, k, value)
        else:
            keys = list(doc)
            nodes_pos = keys.index("nodes") if "nodes" in doc else len(doc)
            # the blank line / comment after the previous entry introduces `nodes`: keep it
            # after the new key
            prev = _entry_slot(doc, keys[nodes_pos - 1]) if nodes_pos > 0 else None
            rest = _detach(prev) if nodes_pos < len(keys) else ""
            # `defaults` / `start` are sections of their own: block style, like `trigger`
            block = k in ("defaults", "start")
            doc.insert(nodes_pos, k, build(value, flow=False if block else None))
            _attach(_entry_slot(doc, k), rest)

    # nodes
    if "nodes" in model:
        nodes = doc.get("nodes")
        if not isinstance(nodes, CommentedMap):
            nodes = CommentedMap()
            if "nodes" in doc:
                doc["nodes"] = nodes
            else:
                _set_key(doc, "nodes", nodes)
        old_nodes = {n["id"]: n for n in old["nodes"]}
        new_nodes = {str(n["id"]): n for n in model["nodes"]}
        # renames (`prev_id`) keep the node's position and comments
        for nid, n in new_nodes.items():
            prev = n.get("prev_id")
            if (
                prev
                and prev != nid
                and prev in nodes
                and prev not in new_nodes
                and nid not in nodes
            ):
                pos = list(nodes).index(prev)
                ca = nodes.ca.items.pop(prev, None)
                value = nodes.pop(prev)
                nodes.insert(pos, nid, value)
                if ca is not None:
                    nodes.ca.items[nid] = ca
                old_nodes[nid] = {**old_nodes.pop(prev), "id": nid}
        for nid in [k for k in nodes if str(k) not in new_nodes]:
            _del_key(nodes, nid)
            old_nodes.pop(str(nid), None)
        for nid, n in new_nodes.items():
            fields = dict(n.get("fields") or {})
            if n.get("type") is not None:
                fields = {"type": n["type"], **{k: v for k, v in fields.items() if k != "type"}}
            if nid in nodes and isinstance(nodes[nid], CommentedMap):
                fields = _respell(nodes[nid], old_nodes[nid]["fields"], fields)
                _update_map(nodes[nid], old_nodes[nid]["fields"], fields)
            elif nid in nodes:
                if not same(plain(nodes[nid]), fields):
                    nodes[nid] = build(fields, flow=False)
            else:
                _set_key(nodes, nid, build(fields, flow=False))

        if not nodes:
            update_value(doc, "nodes", {})

    # edges
    if "edges" in model:
        _apply_edges(doc, old["edges"], list(model["edges"]), indent[2])

    after = _dump(doc, indent)
    if after == before:
        return text, warnings
    out = _collapse_blank_runs(rebase(text, before, after), text)
    lost = _comments(load_document(text)) - _comments(load_document(out))
    lines = text.splitlines()
    for comment in lost:
        where = next((i + 1 for i, x in enumerate(lines) if comment in x), None)
        warnings.append(
            f"comment {comment!r}" + (f" (line {where})" if where else "") + " could not be kept"
        )
    return out, warnings


def _blank_runs(text: str) -> int:
    longest = run = 0
    for line in text.splitlines():
        run = run + 1 if not line.strip() else 0
        longest = max(longest, run)
    return longest


def _collapse_blank_runs(out: str, orig: str) -> str:
    """ruamel sometimes keeps a blank line both before and after a deleted entry; never leave
    longer runs of blank lines than the original file had."""
    limit = max(_blank_runs(orig), 1)
    lines, run = [], 0
    for line in out.splitlines(keepends=True):
        run = run + 1 if not line.strip() else 0
        if run <= limit:
            lines.append(line)
    return "".join(lines)


# -- rebase -----------------------------------------------------------------------------------
def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _rebase_seq(
    orig: Sequence[str],
    base: Sequence[str],
    new: Sequence[str],
    norm: Callable[[str], str],
    refine: Callable[[str, str, str], str] | None,
) -> list[str]:
    """`orig` and `base` say the same thing in different styles; `base` -> `new` is an edit.
    Return `new` in `orig`'s style: every element `new` shares with `base` comes from `orig`."""
    # 1. align orig with base: segments of (base range, orig elements), in order
    segs: list[tuple[int, int, list[str]]] = []
    sm = SequenceMatcher(None, [norm(x) for x in orig], [norm(x) for x in base], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1 and refine is None):
            segs.extend((j1 + d, j1 + d + 1, [orig[i1 + d]]) for d in range(i2 - i1))
        else:
            segs.append((j1, j2, list(orig[i1:i2])))
    one_to_one = {s[0]: s[2][0] for s in segs if s[1] - s[0] == 1 and len(s[2]) == 1}

    # 2. replay base -> new
    out: list[str] = []
    done: set[int] = set()

    def emit_unchanged(j1: int, j2: int) -> None:
        for k, (b1, b2, o) in enumerate(segs):
            if k in done:
                continue
            here = b1 == b2 and j1 <= b1 < j2  # orig-only elements at this position
            inside = b1 < b2 and j1 <= b1 and b2 <= j2
            if here or inside:
                out.extend(o)
                done.add(k)
            elif b1 < b2 and b1 < j2 and b2 > j1:  # partly unchanged: fall back to base
                out.extend(base[max(b1, j1) : min(b2, j2)])

    sm2 = SequenceMatcher(None, list(base), list(new), autojunk=False)
    for tag, j1, j2, k1, k2 in sm2.get_opcodes():
        if tag == "equal":
            emit_unchanged(j1, j2)
        elif tag == "replace" and j2 - j1 == k2 - k1 and refine is not None:
            for d in range(j2 - j1):
                o = one_to_one.get(j1 + d)
                b, n = base[j1 + d], new[k1 + d]
                out.append(refine(o, b, n) if o is not None and o != b else n)
        else:
            out.extend(new[k1:k2])
    for k, (b1, b2, o) in enumerate(segs):  # orig-only elements at the very end
        if k not in done and b1 == b2 == len(base):
            out.extend(o)
    return out


def _rebase_chars(orig: str, base: str, new: str) -> str:
    return "".join(_rebase_seq(list(orig), list(base), list(new), lambda c: c, None))


def rebase(orig: str, base: str, new: str) -> str:
    """Apply the line edit `base` -> `new` onto `orig`, where `base` is ruamel's re-dump of
    `orig`. Unchanged lines keep `orig`'s exact text; changed lines that ruamel restyled
    (`{ a: 1 }` vs `{a: 1}`) get `orig`'s spacing back."""
    if base == new:
        return orig
    o, b, n = (s.splitlines(keepends=True) for s in (orig, base, new))
    return "".join(_rebase_seq(o, b, n, _squash, _rebase_chars))
