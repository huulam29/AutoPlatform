"""Semantic checks on a parsed pipeline (ARCHITECTURE.md §9 "Validator rules")."""

from __future__ import annotations

from typing import Any

from agentline.agents import choose, resolve
from agentline.config import Config
from agentline.errors import ValidationError
from agentline.graph import DEFAULT_WHEN, EdgeDef, Graph, NodeDef, parse_input
from agentline.pipeline.spec import Issue, NodeSpec, PipelineSpec, SpecError
from agentline.pipeline.template import references, value_references
from agentline.registry import Registry

BUILTIN_REFS = {"payload", "run", "node", "pipeline"}
WRITE_TOOLS = ("edit", "bash")  # tools that can change files: need a workspace


async def _never(ctx: Any, inputs: dict[str, Any]) -> Any:  # skeleton graph handler
    raise NotImplementedError


def skeleton(spec: PipelineSpec) -> Graph:
    """The graph shape without handlers (for structural checks)."""
    g = Graph(spec.name, spec.budget_usd)
    for n in spec.nodes.values():
        kind = n.type if n.type in ("router", "gate", "wait") else "step"
        g.add_node(NodeDef(n.id, _never, kind=kind, inputs=n.inputs, out=n.out))  # type: ignore[arg-type]
    for e in spec.edges:
        if e.src in spec.nodes and e.dst in spec.nodes:
            edge = EdgeDef(e.src, e.dst, e.when, e.max_loops, e.on_exhausted)
            if all(x.id != edge.id for x in g.edges):
                g.add_edge(edge)
    return g


class _Validator:
    def __init__(
        self, spec: PipelineSpec, registry: Registry | None, config: Config | None
    ) -> None:
        self.spec = spec
        self.registry = registry
        self.config = config
        self.issues: list[Issue] = []
        self.file = str(spec.path) if spec.path else None

    def add(
        self, path: str, message: str, line: int | None = None, hint: str | None = None
    ) -> None:
        self.issues.append(Issue(path, message, line, 1 if line else None, hint, self.file))

    def node_line(self, node_id: str) -> int | None:
        n = self.spec.nodes.get(node_id)
        return n.line if n else None

    def run(self) -> list[Issue]:
        spec = self.spec
        seen: set[str] = set()
        for i, e in enumerate(spec.edges):
            for end in (e.src, e.dst):
                if end not in spec.nodes:
                    self.add(
                        f"edges[{i}]",
                        f"unknown node {end!r}",
                        e.line,
                        f"nodes are: {', '.join(spec.nodes)}",
                    )
            key = f"{e.src}->{e.dst}[{e.when}]"
            if key in seen:
                self.add(f"edges[{i}]", f"duplicate edge {e.src} -> {e.dst}", e.line)
            seen.add(key)
        if self.issues:
            self.trigger()
            return self.issues
        g = skeleton(spec)
        self.structure(g)
        self.start(g)
        if not self.issues:
            self.data_flow(g)
            self.loop_bodies(g)
        self.defaults()
        for n in spec.nodes.values():
            self.node(n, g)
            self.ai_choice(n)
        self.gates(g)
        self.trigger()
        return self.issues

    def trigger(self) -> None:
        t = self.spec.trigger
        if t.type != "schedule" or not t.schedule:
            return
        from agentline.schedules import CronError, Schedule

        try:
            Schedule.parse(t.schedule, t.timezone)
        except CronError as e:
            what = "timezone" if "timezone" in str(e) else "schedule"
            self.add(
                f"trigger.{what}",
                f"invalid {what}: {e}",
                t.line,
                "cron is 'minute hour day-of-month month day-of-week' or @hourly/@daily/..."
                if what == "schedule"
                else "use an IANA name such as Europe/Berlin, or UTC",
            )

    # -- structure ------------------------------------------------------------------------
    def structure(self, g: Graph) -> None:
        if cycle := g.find_cycle():
            self.add(
                "edges",
                f"cycle without max_loops: {' -> '.join(cycle)}",
                self.node_line(cycle[0]),
                "mark the edge going back with max_loops to make it a bounded loop",
            )
        for i, e in enumerate(self.spec.edges):
            src = self.spec.nodes[e.src]
            if e.when is not None and src.type != "router":
                self.add(
                    f"edges[{i}]",
                    f"'when' on an edge from {src.type} node {e.src!r}",
                    e.line,
                    "only router edges are conditional",
                )
            if src.type == "router" and e.when is None:
                self.add(
                    f"edges[{i}]",
                    f"edge from router {e.src!r} needs 'when'",
                    e.line,
                    f"use a case label, or when: {DEFAULT_WHEN}",
                )
            if e.max_loops is not None:
                if e.max_loops < 1:
                    self.add(f"edges[{i}].max_loops", "must be >= 1", e.line)
                if e.src != e.dst and e.src not in g._forward_reachable(e.dst):
                    self.add(
                        f"edges[{i}]",
                        f"max_loops on {e.src} -> {e.dst}, which doesn't loop back",
                        e.line,
                        "max_loops goes on the edge that returns to an earlier node",
                    )
                if (
                    e.on_exhausted not in ("needs-review", "fail")
                    and e.on_exhausted not in self.spec.nodes
                ):
                    self.add(
                        f"edges[{i}].on_exhausted",
                        f"must be needs-review, fail or a node id, not {e.on_exhausted!r}",
                        e.line,
                    )
            elif e.on_exhausted != "needs-review":
                self.add(f"edges[{i}].on_exhausted", "only valid together with max_loops", e.line)
        for n in self.spec.nodes.values():
            if n.type == "router":
                labels = [e.when for e in g.out_edges(n.id) if e.when]
                if not labels:
                    self.add(f"nodes.{n.id}", "router has no out-edges", n.line)
                dup = {x for x in labels if labels.count(x) > 1}
                if dup:
                    self.add(
                        f"nodes.{n.id}",
                        f"router has duplicate cases: {', '.join(sorted(dup))}",
                        n.line,
                    )
        starts = g.start_nodes()
        if not starts:
            self.add("edges", "no start node: every node has an incoming edge")
            return
        reachable = set(starts)
        for s in starts:
            reachable |= g._forward_reachable(s)
        changed = True
        while changed:
            changed = False
            for e in g.edges:
                if e.src in reachable and (e.loop or e.on_exhausted in g.nodes):
                    extra = {e.dst, *g._forward_reachable(e.dst)}
                    if e.loop and e.on_exhausted in g.nodes:
                        extra |= {e.on_exhausted, *g._forward_reachable(e.on_exhausted)}
                    if not extra <= reachable:
                        reachable |= extra
                        changed = True
        for n in self.spec.nodes:
            if n not in reachable:
                self.add(
                    f"nodes.{n}",
                    "node is unreachable from the start",
                    self.node_line(n),
                    "connect it with an edge or remove it",
                )

    def start(self, g: Graph) -> None:
        """`start.node` is a start node; a chat node is the start node, and the only one (a
        second start node would run before the gatekeeper hands off)."""
        s = self.spec.start
        starts = g.start_nodes()
        chats = [n for n in self.spec.nodes.values() if n.type == "chat"]
        if s.node is not None and s.node not in self.spec.nodes:
            self.add("start.node", f"unknown node {s.node!r}", s.line,
                     f"nodes are: {', '.join(self.spec.nodes)}")  # fmt: skip
            return
        if s.node is not None and s.node not in starts:
            self.add("start.node", f"{s.node!r} has an incoming edge, so it can't take the input",
                     s.line, f"start nodes are: {', '.join(starts) or 'none'}")  # fmt: skip
        for n in chats:
            if len(chats) > 1:
                self.add(f"nodes.{n.id}", "a pipeline has at most one chat node", n.line,
                         "the chat node is the start point; keep one")  # fmt: skip
                continue
            if starts != [n.id]:
                self.add(f"nodes.{n.id}", "a chat node must be the pipeline's only start node",
                         n.line, "connect the other start nodes after it")  # fmt: skip
            if s.node is not None and s.node != n.id:
                self.add("start.node", f"the chat node {n.id!r} is the start point", s.line)
            if s.input not in (None, "chat"):
                self.add("start.input", "a chat start node takes chat input", s.line,
                         "remove input:")  # fmt: skip
        if s.input == "chat" and not chats:
            self.add("start.input", "input: chat needs a chat node", s.line,
                     "add a node with type: chat as the start point")  # fmt: skip
        if s.template is not None and (s.input or "json") != "json":
            self.add("start.template", "template only applies to json input", s.line)

    # -- data flow ------------------------------------------------------------------------
    def topo(self, g: Graph) -> list[str]:
        indeg = {n: len(g.in_edges(n)) for n in g.nodes}
        order, queue = [], [n for n, d in indeg.items() if d == 0]
        while queue:
            n = queue.pop(0)
            order.append(n)
            for e in g.out_edges(n):
                if not e.loop:
                    indeg[e.dst] -= 1
                    if indeg[e.dst] == 0:
                        queue.append(e.dst)
        return order

    def data_flow(self, g: Graph) -> None:
        """A required input must be produced before the node on every path (first iteration);
        an optional one (`x?`) by some node."""
        # A node runs once every incoming edge has delivered a token, with at least one live.
        # An edge can deliver a *skip* only if it leaves a router (another branch was taken) or
        # its source can itself be skipped. Edges that can't be skipped always bring their
        # source's artifacts, so those are guaranteed; with no such edge, only what every
        # incoming path produces is.
        guaranteed: dict[str, set[str]] = {}
        may_skip: dict[str, bool] = {}
        for n in self.topo(g):
            ins = g.in_edges(n)

            def edge_may_skip(e: EdgeDef) -> bool:
                return g.nodes[e.src].kind == "router" or may_skip[e.src]

            sure = [e for e in ins if not edge_may_skip(e)]
            may_skip[n] = bool(ins) and not sure
            before: set[str] = set()
            if ins:
                before = set.intersection(*(guaranteed[e.src] for e in ins))
                for e in sure:
                    before |= guaranteed[e.src]
            node = g.nodes[n]
            spec = self.spec.nodes[n]
            for name in node.inputs:
                artifact, optional = parse_input(name)
                if artifact == "payload":
                    continue
                if not g.producers(artifact):
                    self.add(
                        f"nodes.{n}.in",
                        f"input {artifact!r} is not produced by any node",
                        spec.line,
                        "artifacts are: "
                        + ", ".join(sorted({x.out_name for x in g.nodes.values()})),
                    )
                elif not optional and artifact not in before:
                    self.add(
                        f"nodes.{n}.in",
                        f"input {artifact!r} isn't produced on every path to this node",
                        spec.line,
                        f"make it optional ({artifact}?) or reorder the edges",
                    )
            guaranteed[n] = before | {node.out_name}

    def loop_bodies(self, g: Graph) -> None:
        for e in g.edges:
            if not e.loop:
                continue
            body = {
                n
                for n in g._forward_reachable(e.dst) | {e.dst}
                if n == e.src or e.src in g._forward_reachable(n)
            } | {e.src, e.dst}
            for n in body - {e.dst}:
                for inc in g.in_edges(n):
                    if inc.src not in body:
                        self.add(
                            f"nodes.{n}",
                            f"node inside loop {e.src} -> {e.dst} joins with {inc.src!r}"
                            " outside it",
                            self.node_line(n),
                            "iteration 2 would wait forever; move the join after the loop",
                        )

    # -- per node -------------------------------------------------------------------------
    def defaults(self) -> None:
        d = self.spec.defaults
        if d.harness is None and d.model is None:
            return
        try:
            resolve(d.harness, d.model, self.config)
        except ValidationError as e:
            self.add("defaults", str(e), d.line, "pick from Settings → Harnesses / Models")

    def mcp(self, n: NodeSpec, p: str, kind: str) -> None:
        from agentline.mcp import MCP_KINDS

        known = self.config.mcp if self.config else {}
        missing = [m for m in n.mcp if m not in known]
        if missing:
            self.add(
                f"{p}.mcp",
                f"unknown MCP server(s) {', '.join(missing)}",
                n.line,
                "add them under Settings → MCP"
                + (f" (known: {', '.join(sorted(known))})" if known else ""),
            )
        if kind not in MCP_KINDS:
            self.add(
                f"{p}.mcp",
                f"harness kind {kind!r} can't use MCP servers ({', '.join(MCP_KINDS)} can)",
                n.line,
            )

    def ai_choice(self, n: NodeSpec) -> None:
        """The node's harness + model exist and fit together (Claude Code: Anthropic only)."""
        p = f"nodes.{n.id}"
        d = self.spec.defaults
        try:
            if (n.type == "agent" and not n.runner) or n.type == "chat":
                res = resolve(*choose(n.harness, n.model, d.harness, d.model), self.config)
                if n.mcp:
                    self.mcp(n, p, res.harness.kind)
                if self.registry is not None and res.runner not in self.registry.runners:
                    self.add(
                        p,
                        f"harness {res.harness.name!r} needs runner {res.runner!r},"
                        " which isn't registered",
                        n.line,
                    )
            elif n.type == "router" and n.router_mode == "llm":
                if not (n.model or d.model):
                    self.add(
                        p,
                        "llm router has no model",
                        n.line,
                        "set model: on the router or defaults: { model: ... }",
                    )
                    return
                resolve("llm", n.model or d.model, self.config)
                labels = [e.when for e in self.spec.edges if e.src == n.id and e.when]
                if not [x for x in labels if x != "default"]:
                    self.add(
                        p, "llm router needs out-edges with 'when' labels to choose from", n.line
                    )
        except ValidationError as e:
            self.add(p, str(e), n.line, "pick from Settings → Harnesses / Models")

    def skills(self, n: NodeSpec, p: str) -> None:
        """Skill names are checked against the local library once it has been fetched (before
        that, the run says to fetch it)."""
        from agentline.skills import NAME, SkillLibrary

        bad = [s for s in n.skills if not NAME.match(s)]
        if bad:
            self.add(f"{p}.skills", f"bad skill name(s) {', '.join(bad)}", n.line)
            return
        lib = SkillLibrary()
        known = {s.name for s in lib.list()}
        if not lib.fetched and not known.issuperset(n.skills):
            return  # checked at run time (which says to fetch the library)
        missing = [s for s in n.skills if s not in known]
        if missing:
            self.add(
                f"{p}.skills",
                f"unknown skill(s) {', '.join(missing)} in {lib.repo}",
                n.line,
                "pick from the skill list, or refetch the library",
            )

    def node(self, n: NodeSpec, g: Graph) -> None:
        p = f"nodes.{n.id}"
        available = {parse_input(x)[0] for x in n.inputs} | BUILTIN_REFS
        if n.for_each:
            available |= {"item", "item_index"}
            source = n.for_each.split(".")[0]
            if source not in available - {"item", "item_index"}:
                self.add(
                    f"{p}.for_each",
                    f"for_each reads {source!r}, which isn't a declared input",
                    n.line,
                    f"add {source!r} to 'in'",
                )
        refs: set[str] = set(value_references(n.params))
        if n.prompt:
            refs |= references(n.prompt)
        optional = {parse_input(x)[0] for x in n.inputs if parse_input(x)[1]}
        if optional:
            from agentline.pipeline.template import unguarded_references

            texts = [n.prompt or "", *_strings(n.params)]
            bare = set().union(*(unguarded_references(t) for t in texts)) & optional
            for ref in sorted(bare):
                self.add(
                    p,
                    f"optional input {ref!r} is used outside a {{{{#{ref}?}}}} block; it is"
                    " missing on the first pass and the template fails",
                    n.line,
                    f"wrap it: {{{{#{ref}?}}}} ... {{{{{ref}.field}}}} ... {{{{/{ref}?}}}}",
                )
        for ref in sorted(refs - available):
            self.add(
                p,
                f"template refers to {ref!r}, which isn't a declared input",
                n.line,
                f"add {ref!r} (or {ref}?) to 'in'",
            )
        chat_writers = [t for t in WRITE_TOOLS if t in n.allowed_tools]
        if n.type == "chat" and chat_writers:
            self.add(
                f"{p}.allowed_tools",
                f"a chat node runs on the agentline host: no {', '.join(chat_writers)} tools",
                n.line,
                "use read-only tools; the pipeline's workers do the edits",
            )
        if n.type == "agent" and n.workspace is None:
            writers = [t for t in WRITE_TOOLS if t in n.allowed_tools]
            if writers:
                self.add(
                    f"{p}.allowed_tools",
                    f"agent with {', '.join(writers)} tools has no workspace, so its edits"
                    " would land in the agentline host's working directory",
                    n.line,
                    "add workspace: { type: container, repo: <name> } (or type: local),"
                    " or drop edit/bash from allowed_tools",
                )
        if n.type == "router" and n.switch:
            artifact = n.switch.split(".")[0]
            if artifact not in available:
                self.add(f"{p}.switch", f"switch reads {artifact!r}, which isn't an input", n.line)
        if n.type in ("agent", "chat") and n.skills:
            self.skills(n, p)
        reg = self.registry
        if reg is None:
            return
        if n.type == "agent" and n.runner and n.runner not in reg.runners:
            self.add(
                f"{p}.runner",
                f"unknown runner {n.runner!r}",
                n.line,
                f"known: {', '.join(sorted(reg.runners)) or 'none'}",
            )
        if n.type == "agent" and n.workspace and n.workspace.type not in reg.workspaces:
            self.add(
                f"{p}.workspace",
                f"unknown workspace type {n.workspace.type!r}",
                n.line,
                f"known: {', '.join(sorted(reg.workspaces)) or 'none'}",
            )
        if n.type == "action" and n.uses:
            if n.uses not in reg.actions:
                self.add(
                    f"{p}.uses",
                    f"unknown action {n.uses!r}",
                    n.line,
                    f"known: {', '.join(sorted(reg.actions)) or 'none'}",
                )
            elif self.deploy_class(n) and n.effect.on_unknown == "ai":
                self.add(
                    f"{p}.effect.on_unknown",
                    "on_unknown: ai is not allowed on deploy-class actions",
                    n.line,
                    "use halt (default) or retry",
                )
        if n.type == "wait" and n.wait_for and n.wait_for not in reg.waiters:
            self.add(
                f"{p}.for",
                f"unknown waiter {n.wait_for!r}",
                n.line,
                f"known: {', '.join(sorted(reg.waiters)) or 'none'}",
            )
        allow = self.config.allow_images if self.config else ()
        image = n.workspace.image if n.workspace else "auto"
        if allow and image != "auto" and not any(image.startswith(x) for x in allow):
            self.add(
                f"{p}.workspace.image",
                f"image {n.workspace.image!r} is not on allow_images",
                n.line,
            )

    def deploy_class(self, n: NodeSpec) -> bool:
        if n.type != "action" or not n.uses or self.registry is None:
            return False
        action = self.registry.actions.get(n.uses)
        return bool(action and action.deploy_class)

    def gates(self, g: Graph) -> None:
        """Every deploy-class action must be preceded by a gate on every path."""
        deploys = [n for n in self.spec.nodes.values() if self.deploy_class(n)]
        if not deploys:
            return
        seen: set[str] = set()
        stack = [s for s in g.start_nodes() if self.spec.nodes[s].type != "gate"]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for e in g.out_edges(cur):
                if self.spec.nodes[e.dst].type != "gate":
                    stack.append(e.dst)
        for n in deploys:
            if n.id in seen:
                self.add(
                    f"nodes.{n.id}",
                    f"deploy-class action {n.uses!r} can run without passing a gate",
                    n.line,
                    "put a gate node on every path before it",
                )


def validate_spec(
    spec: PipelineSpec, registry: Registry | None = None, config: Config | None = None
) -> list[Issue]:
    return _Validator(spec, registry, config).run()


def check_spec(
    spec: PipelineSpec, registry: Registry | None = None, config: Config | None = None
) -> None:
    if issues := validate_spec(spec, registry, config):
        raise SpecError(issues)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [x for v in value.values() for x in _strings(v)]
    if isinstance(value, list):
        return [x for v in value for x in _strings(v)]
    return []
