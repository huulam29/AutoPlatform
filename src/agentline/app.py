"""Python API: declare flows of steps, submit runs, act on them."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentline.artifacts import HANDOFF_MAX, INLINE_MAX, ArtifactStore, artifact_dict
from agentline.context import Advisor
from agentline.errors import ValidationError
from agentline.graph import EdgeDef, Graph, Handler, NodeDef, NodeKind
from agentline.ledger.base import AuditEntry, Execution, Ledger, LedgerTx, Run
from agentline.redact import Redactor
from agentline.scheduler import Scheduler
from agentline.worker import Worker


def default_actor() -> str:
    """Who is acting: `$AGENTLINE_USER`, else `$USER`, else `unknown`."""
    return os.environ.get("AGENTLINE_USER") or os.environ.get("USER") or "unknown"


@dataclass(frozen=True)
class Submitted:
    run_id: str
    created: bool


class Flow:
    def __init__(self, graph: Graph) -> None:
        self.graph = graph

    def step(
        self,
        name: str,
        *,
        after: str | Sequence[str] | None = None,
        inputs: Sequence[str] | None = None,
        out: str | None = None,
        kind: NodeKind = "step",
        max_attempts: int = 3,
        timeout: float | None = None,
        budget_usd: float | None = None,
    ) -> Callable[[Handler], Handler]:
        """Register a step. `inputs` defaults to the outputs of the `after` steps."""
        afters = [after] if isinstance(after, str) else list(after or [])

        def register(fn: Handler) -> Handler:
            ins = (
                tuple(inputs)
                if inputs is not None
                else tuple(self.graph.nodes[a].out_name for a in afters)
            )
            self.graph.add_node(
                NodeDef(
                    id=name,
                    handler=fn,
                    kind=kind,
                    inputs=ins,
                    out=out,
                    max_attempts=max_attempts,
                    timeout=timeout,
                    budget_usd=budget_usd,
                )
            )
            for a in afters:
                self.graph.add_edge(EdgeDef(a, name))
            return fn

        return register

    def router(self, name: str, **kw: Any) -> Callable[[Handler], Handler]:
        return self.step(name, kind="router", **kw)

    def edge(
        self,
        src: str,
        dst: str,
        *,
        when: str | None = None,
        max_loops: int | None = None,
        on_exhausted: str = "needs-review",
    ) -> None:
        self.graph.add_edge(EdgeDef(src, dst, when, max_loops, on_exhausted))


class App:
    def __init__(
        self,
        ledger: Ledger,
        *,
        advisor: Advisor | None = None,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        max_reclaims: int = 20,
        artifacts: ArtifactStore | None = None,
        artifact_root: str | Path | None = None,
        inline_max: int = INLINE_MAX,
        handoff_max: int = HANDOFF_MAX,
        redactor: Redactor | None = None,
        incidents: Any = None,
    ) -> None:
        """`artifact_root` defaults to `$AGENTLINE_HOME/artifacts` (`~/.agentline/artifacts`);
        artifacts up to `inline_max` bytes stay in the ledger; inputs over `handoff_max` reach
        handlers as references. Pass `artifacts` to supply a configured store instead.
        `redactor` defaults to one built from the environment (`Redactor.from_env()`); it
        scrubs results, errors, details, attachments and audit rows before they're written."""
        self.ledger = ledger
        self.advisor = advisor
        from agentline.secrets import load_secrets, read_secrets, track_redactor

        load_secrets()  # keys saved from the GUI; variables already in the environment win
        self.redactor = redactor if redactor is not None else Redactor.from_env()
        for name, value in read_secrets().items():
            self.redactor.add(name, value)
        track_redactor(self.redactor)
        # Crash/failure reports (agentline.incidents.IncidentStore); off unless given, so
        # library use and tests never write report files.
        self.incidents = incidents
        self._graphs: dict[str, Graph] = {}
        # Every structure of a flow still in use, by `Graph.structure_hash()`: a run keeps the
        # structure it started with (its `flow_hash`) when a reload changes the pipeline's
        # nodes or edges; a reload that only changes settings replaces the graph of the same
        # structure, so running runs use the new settings (agentline.live).
        self._versions: dict[str, dict[str, Graph]] = {}
        self.runtime: dict[str, Any] = {}  # live status from loops in this process (jira_poll)
        self._validated: set[str] = set()
        self.scheduler = Scheduler(
            self.graph,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
            max_reclaims=max_reclaims,
            artifacts=artifacts or ArtifactStore(artifact_root, inline_max=inline_max),
            handoff_max=handoff_max,
            redactor=self.redactor,
        )

    # -- flows ------------------------------------------------------------------------------
    def flow(self, name: str, *, budget_usd: float | None = None) -> Flow:
        if name in self._graphs:
            raise ValidationError(f"flow {name!r} already registered")
        graph = Graph(name, budget_usd)
        self._graphs[name] = graph
        return Flow(graph)

    def register_pipeline(self, pipeline: Any) -> None:
        """Register a compiled `agentline.pipeline.Pipeline`."""
        self.register(pipeline.graph)

    def flow_names(self) -> list[str]:
        return sorted(self._graphs)

    def register(self, graph: Graph) -> None:
        graph.validate()
        self._graphs[graph.name] = graph
        self._versions.setdefault(graph.name, {})[graph.structure_hash()] = graph
        self._validated.add(graph.name)

    def replace_graphs(self, graphs: dict[str, Graph]) -> dict[str, list[str]]:
        """Swap in freshly compiled graphs (a live reload). Flows not in `graphs` stop taking new
        runs; runs already going keep the structure they started with. Returns what changed:
        {added, removed, restructured, updated}."""
        out: dict[str, list[str]] = {"added": [], "removed": [], "restructured": [], "updated": []}
        for name in sorted(set(self._graphs) - set(graphs)):
            del self._graphs[name]
            out["removed"].append(name)
        for name, graph in sorted(graphs.items()):
            old = self._graphs.get(name)
            self.register(graph)
            if old is None:
                out["added"].append(name)
            elif old.structure_hash() != graph.structure_hash():
                out["restructured"].append(name)
            else:
                out["updated"].append(name)
        self._prune_versions()
        return out

    def _prune_versions(self) -> None:
        """Forget old structures no unfinished run uses any more."""
        from agentline.states import RunStatus

        active = {RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING, RunStatus.FAILED,
                  RunStatus.NEEDS_REVIEW}  # fmt: skip
        with self.ledger.tx() as tx:
            used = {(r.flow, r.flow_hash) for r in tx.list_runs(limit=100_000)
                    if r.status in active}  # fmt: skip
        for name, versions in list(self._versions.items()):
            current = self._graphs.get(name)
            keep = {h: g for h, g in versions.items()
                    if (current is not None and g is current) or (name, h) in used}  # fmt: skip
            if keep:
                self._versions[name] = keep
            else:
                del self._versions[name]

    def graph(self, name: str, run: Any = None) -> Graph:
        """The flow's current graph; for a `run`, the graph with the structure it started
        with when that is still known (else the current one)."""
        if run is not None:
            pinned = self._versions.get(name, {}).get(getattr(run, "flow_hash", None) or "")
            if pinned is not None:
                return pinned
        graph = self._graphs.get(name)
        if graph is None and run is not None and self._versions.get(name):
            graph = next(iter(self._versions[name].values()))  # the pipeline was removed
        if graph is None:
            raise ValidationError(f"unknown flow {name!r}")
        if name not in self._validated:
            graph.validate()
            self._validated.add(name)
        return graph

    def worker(self, **kw: Any) -> Worker:
        return Worker(self, **kw)

    # -- runs -------------------------------------------------------------------------------
    async def submit(
        self,
        flow: str,
        payload: Any,
        *,
        dedupe_key: str | None = None,
        budget_usd: float | None = None,
    ) -> Submitted:
        return await asyncio.to_thread(self.submit_sync, flow, payload, dedupe_key, budget_usd)

    def submit_sync(
        self,
        flow: str,
        payload: Any,
        dedupe_key: str | None = None,
        budget_usd: float | None = None,
    ) -> Submitted:
        self.graph(flow)
        with self.ledger.tx() as tx:
            run_id, created = self.scheduler.submit(tx, flow, payload, dedupe_key, budget_usd)
        return Submitted(run_id, created)

    async def deliver_event(self, correlation_key: str, payload: Any) -> list[str]:
        return await asyncio.to_thread(
            self._in_tx, self.scheduler.deliver_event, correlation_key, payload
        )

    async def resolve(
        self,
        exec_id: str,
        action: str,
        *,
        actor: str | None = None,
        audit_detail: dict[str, Any] | None = None,
        **kw: Any,
    ) -> None:
        """Resolve a needs-review execution (`retry|skip|fail|done`), audited in the same tx.
        `kw`: `result`, `effect_decision`, `effect_result` (see Scheduler.resolve)."""
        who = actor or default_actor()

        def _do(tx: LedgerTx) -> None:
            ex = tx.get_execution(exec_id)
            self.scheduler.resolve(tx, exec_id, action, **kw)
            detail: dict[str, Any] = {
                "action": action,
                "execution_id": exec_id,
                "node": ex.key if ex else None,
                "reason": ex.reason if ex else None,
                **{k: v for k, v in kw.items() if v is not None},
                **(audit_detail or {}),
            }
            self._audit(tx, who, "resolve", ex.run_id if ex else None, ex and ex.node_id, detail)

        await asyncio.to_thread(self._in_tx, _do)

    async def cancel(self, run_id: str, *, actor: str | None = None) -> None:
        who = actor or default_actor()

        def _do(tx: LedgerTx) -> None:
            run = tx.get_run(run_id)
            if run is None:
                raise ValidationError(f"no run {run_id}")
            self.scheduler.cancel(tx, run_id)
            self._audit(tx, who, "cancel", run_id, None, {"from_status": run.status})

        await asyncio.to_thread(self._in_tx, _do)

    async def retry(
        self,
        run_id: str,
        *,
        node_id: str | None = None,
        actor: str | None = None,
        via: str | None = None,
    ) -> int:
        """Retry the run's dead executions, or only `node_id`'s (dead or needs-review)."""
        who = actor or default_actor()

        def _do(tx: LedgerTx) -> int:
            run = tx.get_run(run_id)
            if run is None:
                raise ValidationError(f"no run {run_id}")
            if run.status == "cancelled":  # its steps would run while it shows cancelled
                raise ValidationError(f"run {run_id} is cancelled; start a new run instead")
            n = self.scheduler.retry_run(tx, run_id, node_id)
            detail: dict[str, Any] = {"executions": n}
            if via:
                detail["via"] = via
            self._audit(tx, who, "retry", run_id, node_id, detail)
            tx.add_journal(run_id, "retry", f"retry by {who}" + (f" via {via}" if via else ""),
                           node_id=node_id, detail=detail)  # fmt: skip
            return n

        return await asyncio.to_thread(self._in_tx, _do)  # type: ignore[no-any-return]

    def waiting_gates(self, run_id: str, node_id: str | None = None) -> list[Execution]:
        with self.ledger.tx() as tx:
            return self._waiting_gates(tx, run_id, node_id)

    @staticmethod
    def _waiting_gates(tx: LedgerTx, run_id: str, node_id: str | None) -> list[Execution]:
        return [
            x
            for x in tx.list_executions(run_id)
            if x.status == "waiting"
            and x.detail.get("kind") == "gate"
            and node_id in (None, x.node_id)
        ]

    async def approve(
        self,
        run_id: str,
        node_id: str | None = None,
        *,
        actor: str | None = None,
        comment: str = "",
        via: str | None = None,
    ) -> Execution:
        """Approve the run's waiting gate (the only one, or `node_id`). The event delivery and
        the audit row commit in one transaction."""
        who = actor or default_actor()

        def _do(tx: LedgerTx) -> Execution:
            gate = self._one_gate(tx, run_id, node_id, who)
            payload = {"approved": True, "by": who, "comment": comment, "at": time.time()}
            self.scheduler.deliver_event(tx, gate.correlation_key or "", payload)
            detail = {"gate": gate.key, "execution_id": gate.id, "comment": comment}
            if via:
                detail["via"] = via
            self._audit(tx, who, "approve", run_id, gate.node_id, detail)
            return gate

        return await asyncio.to_thread(self._in_tx, _do)  # type: ignore[no-any-return]

    async def reject(
        self,
        run_id: str,
        node_id: str | None = None,
        *,
        actor: str | None = None,
        comment: str = "",
        via: str | None = None,
    ) -> Execution:
        who = actor or default_actor()

        def _do(tx: LedgerTx) -> Execution:
            gate = self._one_gate(tx, run_id, node_id, who)
            msg = f"gate {gate.node_id} rejected by {who}" + (f": {comment}" if comment else "")
            self.scheduler.reject(tx, gate.id, msg)
            detail = {"gate": gate.key, "execution_id": gate.id, "comment": comment}
            if via:
                detail["via"] = via
            self._audit(tx, who, "reject", run_id, gate.node_id, detail)
            return gate

        return await asyncio.to_thread(self._in_tx, _do)  # type: ignore[no-any-return]

    def _one_gate(self, tx: LedgerTx, run_id: str, node_id: str | None, actor: str) -> Execution:
        if tx.get_run(run_id) is None:
            raise ValidationError(f"no run {run_id}")
        gates = self._waiting_gates(tx, run_id, node_id)
        if not gates:
            raise ValidationError(f"run {run_id} has no gate waiting for approval")
        if len(gates) > 1:
            names = ", ".join(g.node_id for g in gates)
            raise ValidationError(f"run {run_id} has several waiting gates ({names}); name one")
        gate = gates[0]
        approvers = gate.detail.get("approvers") or []
        if approvers and actor not in approvers:
            raise PermissionError(f"{actor} is not an approver for {gate.node_id} ({approvers})")
        return gate

    # -- audit ------------------------------------------------------------------------------
    def _audit(
        self,
        tx: LedgerTx,
        actor: str,
        action: str,
        run_id: str | None,
        node_id: str | None,
        detail: dict[str, Any],
    ) -> int:
        return tx.add_audit(
            actor, action, run_id, node_id, self.redactor.redact_value(detail), time.time()
        )

    def audit(
        self,
        action: str,
        detail: dict[str, Any],
        *,
        actor: str | None = None,
        run_id: str | None = None,
        node_id: str | None = None,
    ) -> int:
        """Append an audit row in its own transaction (redacted)."""
        with self.ledger.tx() as tx:
            return self._audit(tx, actor or default_actor(), action, run_id, node_id, detail)

    def audit_log(self, run_id: str | None = None, limit: int = 1000) -> list[AuditEntry]:
        with self.ledger.tx() as tx:
            return tx.list_audit(run_id, limit)

    def _in_tx(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        with self.ledger.tx() as tx:
            return fn(tx, *args, **kw)

    # -- reads ------------------------------------------------------------------------------
    def get_run(self, run_id: str) -> Run | None:
        with self.ledger.tx() as tx:
            return tx.get_run(run_id)

    def executions(self, run_id: str) -> list[Execution]:
        with self.ledger.tx() as tx:
            return tx.list_executions(run_id)

    def result(self, run_id: str, node_id: str, iteration: int | None = None) -> Any:
        """Result of a node's latest (or given) done iteration."""
        with self.ledger.tx() as tx:
            return self._result(tx, run_id, node_id, iteration)

    def _result(self, tx: LedgerTx, run_id: str, node_id: str, iteration: int | None) -> Any:
        execs = [
            x
            for x in tx.list_executions(run_id)
            if x.node_id == node_id and x.status == "done" and (iteration in (None, x.iteration))
        ]
        if not execs:
            return None
        return self.scheduler.read_result(tx, max(execs, key=lambda x: x.iteration))

    # -- artifacts and lineage ---------------------------------------------------------------
    def artifact(self, digest: str) -> dict[str, Any] | None:
        """Metadata of an artifact plus its `value` (JSON value, or bytes for files)."""
        store = self.scheduler.artifacts
        with self.ledger.tx() as tx:
            meta = store.meta(tx, digest)
            if meta is None:
                return None
            return {**artifact_dict(meta), "value": store.load(tx, digest)}

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        """Every artifact a run's executions wrote, in write order, with metadata."""
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        with self.ledger.tx() as tx:
            for link in tx.list_artifact_links(run_id):
                ident = (link.execution_id, link.name, link.hash)
                if link.direction != "wrote" or ident in seen:
                    continue
                seen.add(ident)
                meta = tx.get_artifact(link.hash)
                out.append(
                    {
                        "name": link.name,
                        "execution_id": link.execution_id,
                        "node": f"{link.node_id}#{link.iteration}",
                        **(artifact_dict(meta) if meta else {"hash": link.hash}),
                    }
                )
        return out

    def lineage(self, run_id: str) -> list[dict[str, Any]]:
        """Each execution of the run with what it read and wrote: name, hash, summary, size.
        Reads also name their source execution (`from`, e.g. `implement#2`)."""
        with self.ledger.tx() as tx:
            execs = tx.list_executions(run_id)
            links = tx.list_artifact_links(run_id)
            metas = {h: tx.get_artifact(h) for h in {k.hash for k in links}}
        by_id = {x.id: x for x in execs}
        out: list[dict[str, Any]] = []
        for x in execs:
            entry: dict[str, Any] = {
                "execution_id": x.id,
                "node": x.key,
                "node_id": x.node_id,
                "iteration": x.iteration,
                "status": x.status,
                "reads": [],
                "writes": [],
            }
            for k in links:
                if k.execution_id != x.id:
                    continue
                meta = metas.get(k.hash)
                item: dict[str, Any] = {
                    "name": k.name,
                    "hash": k.hash,
                    "summary": meta.summary if meta else "",
                    "size": meta.size if meta else None,
                }
                if k.direction == "read":
                    src = by_id.get(k.source_execution_id or "")
                    item["from"] = src.key if src else None
                    entry["reads"].append(item)
                else:
                    entry["writes"].append(item)
            out.append(entry)
        return out
