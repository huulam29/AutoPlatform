"""The graph every run executes: nodes with handlers, and edges between them.

The Python API (`Flow`) and the YAML pipeline compiler both build a `Graph`, so there is one
engine with two front-ends.

Edges are control flow. A node becomes ready once every non-loop in-edge has delivered a
token: *live* (the predecessor ran and chose this edge) or *skip* (the predecessor was skipped
or a router chose another edge). A node with only skip tokens is itself skipped, and passes
skips on (dead-path elimination), so joins after a router never wait forever. Loop edges
(`max_loops`) start a new iteration of their target instead.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from agentline.errors import ValidationError
from agentline.ids import canonical_json, content_hash

if TYPE_CHECKING:
    from agentline.context import StepContext

Handler = Callable[["StepContext", dict[str, Any]], Awaitable[Any]]
NodeKind = Literal["step", "router", "gate", "wait"]
DEFAULT_WHEN = "default"
ON_EXHAUSTED = ("needs-review", "fail")


@dataclass
class NodeDef:
    id: str
    handler: Handler
    kind: NodeKind = "step"
    inputs: tuple[str, ...] = ()  # artifact names; "name?" means optional
    out: str | None = None  # artifact name this node writes (default: node id)
    max_attempts: int = 3
    timeout: float | None = None
    budget_usd: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def out_name(self) -> str:
        return self.out or self.id


@dataclass(frozen=True)
class EdgeDef:
    src: str
    dst: str
    when: str | None = None
    max_loops: int | None = None
    on_exhausted: str = "needs-review"  # "needs-review" | "fail" | <node id>

    @property
    def id(self) -> str:
        return f"{self.src}->{self.dst}" + (f"[{self.when}]" if self.when else "")

    @property
    def loop(self) -> bool:
        return self.max_loops is not None


def parse_input(name: str) -> tuple[str, bool]:
    """'review?' -> ('review', True)."""
    return (name[:-1], True) if name.endswith("?") else (name, False)


class Graph:
    def __init__(self, name: str, budget_usd: float | None = None) -> None:
        self.name = name
        self.budget_usd = budget_usd
        self.nodes: dict[str, NodeDef] = {}
        self.edges: list[EdgeDef] = []
        self.meta: dict[str, Any] = {}

    def add_node(self, node: NodeDef) -> NodeDef:
        if node.id in self.nodes:
            raise ValidationError(f"duplicate node id {node.id!r}")
        self.nodes[node.id] = node
        return node

    def add_edge(self, edge: EdgeDef) -> EdgeDef:
        if any(e.id == edge.id for e in self.edges):
            raise ValidationError(f"duplicate edge {edge.id}")
        self.edges.append(edge)
        return edge

    def out_edges(self, node_id: str) -> list[EdgeDef]:
        return [e for e in self.edges if e.src == node_id]

    def in_edges(self, node_id: str, loops: bool = False) -> list[EdgeDef]:
        return [e for e in self.edges if e.dst == node_id and e.loop == loops]

    def start_nodes(self) -> list[str]:
        return [n for n in self.nodes if not self.in_edges(n)]

    def producers(self, artifact: str) -> list[str]:
        return [n.id for n in self.nodes.values() if n.out_name == artifact]

    def structure_hash(self) -> str:
        shape = {
            "nodes": {
                n.id: [n.kind, list(n.inputs), n.out_name, n.max_attempts]
                for n in self.nodes.values()
            },
            "edges": [[e.src, e.dst, e.when, e.max_loops, e.on_exhausted] for e in self.edges],
        }
        return content_hash(canonical_json(shape).encode())[:16]

    def _forward_reachable(self, start: str) -> set[str]:
        seen: set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            for e in self.out_edges(cur):
                if not e.loop and e.dst not in seen:
                    seen.add(e.dst)
                    stack.append(e.dst)
        return seen

    def find_cycle(self) -> list[str] | None:
        """A cycle made only of non-loop edges, as a node path, or None."""
        color: dict[str, int] = dict.fromkeys(self.nodes, 0)
        path: list[str] = []

        def visit(n: str) -> list[str] | None:
            color[n] = 1
            path.append(n)
            for e in self.out_edges(n):
                if e.loop:
                    continue
                if color[e.dst] == 1:
                    return [*path[path.index(e.dst) :], e.dst]
                if color[e.dst] == 0 and (found := visit(e.dst)):
                    return found
            path.pop()
            color[n] = 2
            return None

        for n in self.nodes:
            if color[n] == 0 and (found := visit(n)):
                return found
        return None

    def validate(self) -> None:
        errors: list[str] = []
        if not self.nodes:
            errors.append("graph has no nodes")
        for e in self.edges:
            for end in (e.src, e.dst):
                if end not in self.nodes:
                    errors.append(f"edge {e.id}: unknown node {end!r}")
        if errors:
            raise ValidationError("; ".join(errors))

        if cycle := self.find_cycle():
            errors.append(
                f"cycle without max_loops: {' -> '.join(cycle)} "
                "(mark the back edge with max_loops to make it a bounded loop)"
            )
        for e in self.edges:
            src = self.nodes[e.src]
            if e.when is not None and src.kind != "router":
                errors.append(f"edge {e.id}: only router edges can have 'when'")
            if e.loop:
                if e.max_loops is not None and e.max_loops < 1:
                    errors.append(f"edge {e.id}: max_loops must be >= 1")
                if e.src != e.dst and e.src not in self._forward_reachable(e.dst):
                    errors.append(f"edge {e.id}: max_loops on an edge that doesn't close a loop")
                if e.on_exhausted not in ON_EXHAUSTED and e.on_exhausted not in self.nodes:
                    errors.append(f"edge {e.id}: on_exhausted must be needs-review, fail or a node")
        for n in self.nodes.values():
            if n.kind == "router":
                whens = [e.when for e in self.out_edges(n.id)]
                if not whens:
                    errors.append(f"router {n.id!r} has no out-edges")
                if None in whens:
                    errors.append(f"router {n.id!r}: every out-edge needs 'when'")
                labels = [w for w in whens if w is not None]
                if len(labels) != len(set(labels)):
                    errors.append(f"router {n.id!r}: duplicate 'when' labels")
            for name in n.inputs:
                artifact, _ = parse_input(name)
                if artifact != "payload" and not self.producers(artifact):
                    errors.append(f"node {n.id!r}: input {artifact!r} is not produced by any node")
        starts = self.start_nodes()
        if not starts and self.nodes:
            errors.append("graph has no start node (every node has an incoming edge)")
        reachable = set(starts)
        for s in starts:
            reachable |= self._forward_reachable(s)
        for e in self.edges:  # loop edges and on_exhausted targets also reach nodes
            if e.loop and e.src in reachable:
                reachable |= {e.dst, *self._forward_reachable(e.dst)}
        if unreachable := sorted(set(self.nodes) - reachable):
            errors.append(f"unreachable nodes: {', '.join(unreachable)}")
        if errors:
            raise ValidationError("; ".join(errors))
