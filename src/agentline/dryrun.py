"""Dry run: execute a pipeline on the real engine with every side effect stubbed out.

Agents, actions and workspaces are replaced by stubs; nothing is called, nothing is written
outside a scratch ledger. What stays real is what a dry run is for: the graph, joins, loops
and their `max_loops`, `for_each` fan-out, templates, validation and the journal.

- An agent's stub output follows its `output_schema` (the first enum value, "dry-run" strings,
  0, one-item lists) plus every `artifact.field` later templates read.
- Routers follow `routes: {router_id: [label, label, ...]}` (one label per visit) and
  otherwise take the first label that doesn't loop back (else `default`).
- Gates are approved; waits get `events.get(node_id, {})`; `for_each` lists get two items.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from agentline.app import App
from agentline.ledger.sqlite import SqliteLedger
from agentline.pipeline import build_pipeline
from agentline.pipeline.spec import PipelineSpec
from agentline.registry import ActionCall, Registry, RunRequest, RunResult
from agentline.results import Route
from agentline.runners.function import FunctionRunner

MAX_ROUNDS = 60
_REF = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")


def referenced_paths(spec: PipelineSpec) -> dict[str, set[str]]:
    """artifact -> the field paths later templates and for_each read from it."""
    import json

    texts: list[str] = []
    for n in spec.nodes.values():
        texts += [n.prompt or "", json.dumps(n.params, default=str)]
        if n.switch:
            texts.append("{{" + n.switch + "}}")
    out: dict[str, set[str]] = {}
    for t in texts:
        for m in _REF.finditer(t):
            head, _, rest = m.group(1).partition(".")
            out.setdefault(head, set())
            if rest:
                out[head].add(rest)
    return out


def from_schema(schema: Any, label: str) -> Any:
    if not isinstance(schema, dict):
        return f"dry-run {label}"
    if schema.get("enum"):
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "string")
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {k: from_schema(v, f"{label}.{k}") for k, v in props.items()}
    if t == "array":
        return [from_schema(schema.get("items") or {}, f"{label}[0]")]
    if t in ("integer", "number"):
        return 0
    if t == "boolean":
        return True
    return f"dry-run {label}"


def _set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur.setdefault(parts[-1], value)


def stub_output(spec: PipelineSpec, node_id: str, refs: dict[str, set[str]]) -> Any:
    n = spec.nodes[node_id]
    base = from_schema(n.output_schema, node_id) if n.output_schema else {}
    if not isinstance(base, dict):
        return base
    base.setdefault("dry_run", True)
    lists = {
        x.for_each.split(".", 1)[1]
        for x in spec.nodes.values()
        if x.for_each and x.for_each.split(".")[0] == (n.out or "") and "." in x.for_each
    }
    for path in sorted(refs.get(n.out or "", set())):
        if path in lists:
            _set_path(base, path, [f"{n.out}.{path}[0]", f"{n.out}.{path}[1]"])
        else:
            _set_path(base, path, f"<{n.out}.{path}>")
    return base


class _StubWorkspace:
    def __init__(self, name: str, root: Path) -> None:
        self.name = name
        self.root = root

    def acquire(self, ctx: Any, spec: Any) -> Any:
        root = self.root

        @contextlib.asynccontextmanager
        async def cm() -> AsyncIterator[Any]:
            class WS:
                path = root
                host_path = root

                async def exec(self, cmd: list[str], **kw: Any) -> tuple[int, str, str]:
                    return 0, "", ""

            yield WS()

        return cm()


def stub_registry(
    real: Registry, spec: PipelineSpec, scratch: Path, calls: list[dict[str, Any]]
) -> Registry:
    refs = referenced_paths(spec)
    reg = Registry()

    def record(kind: str, node: str, what: str) -> None:
        calls.append({"at": time.time(), "kind": kind, "node": node, "what": what})

    async def agent(req: RunRequest) -> RunResult:
        record("agent", req.node_id, req.prompt[:300])
        return RunResult(output=stub_output(spec, req.node_id, refs))

    for name in real.runners:
        reg.add_runner(FunctionRunner(name, agent))

    for name, action in real.actions.items():

        def make(action_name: str, deploy: bool) -> Any:
            class Stub:
                def __init__(self) -> None:
                    self.name = action_name
                    self.deploy_class = deploy

                async def __call__(self, ctx: Any, call: ActionCall) -> Any:
                    record("action", ctx.node_id, f"{action_name} {call.params}"[:300])
                    return stub_output(spec, ctx.node_id, refs)

            return Stub()

        reg.add_action(make(name, bool(getattr(action, "deploy_class", False))))
    from agentline.registry import KeyWaiter

    for name in real.waiters:  # the dry run delivers every wait itself: unique keys only
        reg.add_waiter(
            KeyWaiter(
                name, lambda p, i: f"dryrun:{p.get('_run')}:{p.get('_node')}:{p.get('_iteration')}"
            )
        )
    for name in real.workspaces:
        reg.add_workspace(_StubWorkspace(name, scratch))
    return reg


def _router_labels(spec: PipelineSpec, node_id: str) -> list[tuple[str, bool]]:
    """(label, loops back) for each labelled out-edge."""
    return [(str(e.when), bool(e.max_loops)) for e in spec.edges if e.src == node_id and e.when]


async def dry_run(
    spec: PipelineSpec,
    real: Registry,
    config: Any = None,
    *,
    payload: Any = None,
    routes: dict[str, list[str]] | None = None,
    events: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    import copy

    spec = copy.deepcopy(spec)
    skipped = {"skills": {}, "mcp": {}}  # what a real run would load, not needed here
    for n in spec.nodes.values():
        if n.skills:
            skipped["skills"][n.id] = list(n.skills)
            n.skills = ()
        if n.mcp:
            skipped["mcp"][n.id] = list(n.mcp)
            n.mcp = ()
    routes = {k: list(v) for k, v in (routes or {}).items()}
    events = events or {}
    calls: list[dict[str, Any]] = []
    taken: list[dict[str, Any]] = []
    refs = referenced_paths(spec)
    with tempfile.TemporaryDirectory(prefix="agentline-dryrun-") as tmp:
        scratch = Path(tmp)
        reg = stub_registry(real, spec, scratch, calls)
        pipeline = build_pipeline(spec, reg, config)
        for node_id, node in pipeline.graph.nodes.items():
            if spec.nodes[node_id].type == "chat":  # the gatekeeper hands off at once

                def handoff(nid: str) -> Any:
                    async def handler(ctx: Any, inputs: dict[str, Any]) -> Any:
                        calls.append(
                            {
                                "at": time.time(),
                                "kind": "chat",
                                "node": nid,
                                "what": "hands off at once (dry run)",
                            }
                        )
                        return stub_output(spec, nid, refs)

                    return handler

                node.handler = handoff(node_id)
                continue
            if spec.nodes[node_id].type != "router":
                continue
            labels = _router_labels(spec, node_id)

            def chooser(nid: str, labs: list[tuple[str, bool]]) -> Any:
                async def handler(ctx: Any, inputs: dict[str, Any]) -> Route:
                    script = routes.get(nid)
                    if script:
                        label = script.pop(0)
                    else:
                        forward = [lab for lab, loop in labs if not loop and lab != "default"]
                        label = forward[0] if forward else (labs[0][0] if labs else "default")
                    taken.append({"router": nid, "label": label, "iteration": ctx.iteration})
                    return Route(label, {"dry_run": True})

                return handler

            node.handler = chooser(node_id, labels)
        ledger = SqliteLedger(scratch / "dry.db")
        app = App(ledger, backoff_base=0.0)
        app.register_pipeline(pipeline)
        start = dict(payload) if isinstance(payload, dict) else {}
        for path in sorted(refs.get("payload", set())):  # what templates read, if not given
            _set_path(start, path, f"<payload.{path}>")
        sub = app.submit_sync(
            spec.name, start if isinstance(payload, dict) or payload is None else payload
        )
        deadline = time.monotonic() + timeout
        rounds = 0
        try:
            while time.monotonic() < deadline and rounds < MAX_ROUNDS:
                rounds += 1
                await asyncio.wait_for(
                    app.worker(concurrency=4).run_until_idle(),
                    max(1.0, deadline - time.monotonic()),
                )
                run = app.get_run(sub.run_id)
                if run is None or run.status in ("done", "dead", "cancelled", "needs-review"):
                    break
                progressed = False
                for x in app.executions(sub.run_id):
                    if x.status != "waiting":
                        continue
                    if x.detail.get("kind") == "gate":
                        await app.approve(sub.run_id, x.node_id, actor="dry-run", comment="dry run")
                    else:
                        payload_for = events.get(x.node_id) or stub_output(spec, x.node_id, refs)
                        await app.deliver_event(x.correlation_key or "", payload_for)
                    progressed = True
                if not progressed:
                    break
            run = app.get_run(sub.run_id)
            execs = app.executions(sub.run_id)
            with ledger.tx() as tx:
                journal = tx.list_journal(sub.run_id)
                tasks = tx.list_tasks(sub.run_id)
        finally:
            ledger.close()
    latest: dict[str, Any] = {}
    counts: dict[str, int] = {}
    for x in execs:
        if x.status not in ("skipped", "pending"):  # visits that actually ran
            counts[x.node_id] = counts.get(x.node_id, 0) + 1
        if x.node_id not in latest or x.iteration >= latest[x.node_id].iteration:
            latest[x.node_id] = x
    return {
        "status": run.status if run else "unknown",
        "error": run.error if run else None,
        "rounds": rounds,
        "nodes": [
            {
                "node": nid,
                "status": latest[nid].status if nid in latest else "not-started",
                "runs": counts.get(nid, 0),
                "error": latest[nid].error if nid in latest else None,
            }
            for nid in spec.nodes
        ],
        "routes": taken,
        "calls": calls,
        "not_loaded": skipped,
        "tasks": len(tasks),
        "journal": [
            {
                "at": e.at,
                "node": e.node_id,
                "iteration": e.iteration,
                "task": e.task,
                "kind": e.kind,
                "message": e.message,
            }
            for e in journal
        ],
    }
