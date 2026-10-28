from __future__ import annotations

from typing import Any

import pytest

from agentline.errors import ValidationError
from agentline.graph import EdgeDef, Graph, NodeDef


async def noop(ctx: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    return {}


def graph(*nodes: str | tuple[str, str], edges: list[EdgeDef], **kw: Any) -> Graph:
    g = Graph("g")
    for n in nodes:
        node_id, kind = (n, "step") if isinstance(n, str) else n
        g.add_node(NodeDef(node_id, noop, kind=kind, **kw.get(node_id, {})))  # type: ignore[arg-type]
    for e in edges:
        g.add_edge(e)
    return g


def test_linear_graph_is_valid() -> None:
    g = graph("a", "b", "c", edges=[EdgeDef("a", "b"), EdgeDef("b", "c")])
    g.validate()
    assert g.start_nodes() == ["a"]


def test_cycle_without_max_loops_is_rejected_with_path() -> None:
    g = graph("a", "b", "c", edges=[EdgeDef("a", "b"), EdgeDef("b", "c"), EdgeDef("c", "b")])
    with pytest.raises(ValidationError, match=r"cycle without max_loops: b -> c -> b"):
        g.validate()


def test_bounded_loop_is_accepted() -> None:
    g = graph(
        "a",
        "b",
        ("r", "router"),
        "done",
        edges=[
            EdgeDef("a", "b"),
            EdgeDef("b", "r"),
            EdgeDef("r", "b", when="again", max_loops=3),
            EdgeDef("r", "done", when="ok"),
        ],
    )
    g.validate()


def test_max_loops_on_forward_edge_is_rejected() -> None:
    g = graph(("r", "router"), "b", edges=[EdgeDef("r", "b", when="x", max_loops=2)])
    with pytest.raises(ValidationError, match="doesn't close a loop"):
        g.validate()


def test_when_on_non_router_edge_is_rejected() -> None:
    g = graph("a", "b", edges=[EdgeDef("a", "b", when="x")])
    with pytest.raises(ValidationError, match="only router edges"):
        g.validate()


def test_router_edges_need_distinct_labels() -> None:
    g = graph(
        ("r", "router"),
        "a",
        "b",
        edges=[EdgeDef("r", "a", when="x"), EdgeDef("r", "b", when="x")],
    )
    with pytest.raises(ValidationError, match="duplicate 'when'"):
        g.validate()


def test_unknown_node_in_edge() -> None:
    g = graph("a", edges=[EdgeDef("a", "ghost")])
    with pytest.raises(ValidationError, match="unknown node 'ghost'"):
        g.validate()


def test_input_must_be_produced() -> None:
    g = graph("a", "b", edges=[EdgeDef("a", "b")], b={"inputs": ("nope",)})
    with pytest.raises(ValidationError, match="input 'nope' is not produced"):
        g.validate()


def test_optional_input_from_later_node_is_fine() -> None:
    g = graph("a", "b", edges=[EdgeDef("a", "b")], a={"inputs": ("b?",)})
    g.validate()


def test_structure_hash_changes_with_shape() -> None:
    g1 = graph("a", "b", edges=[EdgeDef("a", "b")])
    g2 = graph("a", "b", edges=[EdgeDef("a", "b")])
    g3 = graph("a", "b", "c", edges=[EdgeDef("a", "b"), EdgeDef("a", "c")])
    assert g1.structure_hash() == g2.structure_hash() != g3.structure_hash()
