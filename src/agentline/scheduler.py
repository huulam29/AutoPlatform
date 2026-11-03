"""Scheduling logic: every state change of a run, as functions over one ledger transaction.

Nothing here does I/O except through `tx`, and every function is called inside a single
`ledger.tx()`, so each transition (e.g. "execution done + successors ready + run status") is
atomic. Writes by a worker are fenced by `expect_epoch`.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentline.artifacts import HANDOFF_MAX, ArtifactStore
from agentline.errors import PermanentError, StaleLease, ValidationError
from agentline.graph import DEFAULT_WHEN, EdgeDef, Graph, NodeDef, parse_input
from agentline.ids import new_id
from agentline.ledger.base import ArtifactLink, Execution, LedgerTx, Run
from agentline.redact import Redactor
from agentline.states import (
    TERMINAL_EXEC,
    ExecStatus,
    RunStatus,
    check_exec_transition,
    derive_run_status,
)

log = logging.getLogger("agentline.scheduler")
E = ExecStatus


@dataclass(frozen=True)
class Fence:
    """What the caller believes the execution's state is. The write fails otherwise."""

    status: str
    epoch: int | None = None


class Scheduler:
    def __init__(
        self,
        graphs: Callable[..., Graph],  # (flow, run=None) -> the graph (App.graph)
        *,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        max_reclaims: int = 20,
        clock: Callable[[], float] = time.time,
        artifacts: ArtifactStore | None = None,
        handoff_max: int = HANDOFF_MAX,
        redactor: Redactor | None = None,
    ) -> None:
        self.graphs = graphs
        # every result, error and detail is scrubbed of known secrets before it is written
        self.redactor = redactor if redactor is not None else Redactor()
        self.artifacts = artifacts if artifacts is not None else ArtifactStore()
        self.handoff_max = handoff_max  # inputs larger than this reach handlers as a reference
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_reclaims = max_reclaims
        self.clock = clock

    # -- submission -----------------------------------------------------------------------
    def submit(
        self,
        tx: LedgerTx,
        flow: str,
        payload: Any,
        dedupe_key: str | None = None,
        budget_usd: float | None = None,
    ) -> tuple[str, bool]:
        """Create a run, or return the existing one with the same dedupe_key."""
        if dedupe_key is not None and (existing := tx.get_run_by_dedupe(dedupe_key)):
            return existing.id, False
        graph = self.graphs(flow)
        now = self.clock()
        payload = self.redactor.redact_value(payload)
        run = Run(
            id=new_id("r"),
            flow=flow,
            flow_hash=graph.structure_hash(),
            payload=payload,
            status=RunStatus.PENDING,
            dedupe_key=dedupe_key,
            budget_usd=budget_usd if budget_usd is not None else graph.budget_usd,
            created_at=now,
            updated_at=now,
        )
        tx.insert_run(run)
        for node_id in graph.start_nodes():
            self._insert(
                tx,
                Execution(
                    id=new_id("x"),
                    run_id=run.id,
                    node_id=node_id,
                    iteration=1,
                    status=E.READY,
                    tokens_needed=0,
                    created_at=now,
                ),
            )
        return run.id, True

    # -- worker results -------------------------------------------------------------------
    def complete(
        self,
        tx: LedgerTx,
        ex: Execution,
        result: Any,
        fence: Fence,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Mark the execution done, send tokens along its out-edges, refresh the run."""
        result = self.redactor.redact_value(result)
        detail = self.redactor.redact_value(detail)
        run = self._run(tx, ex.run_id)
        graph = self.graphs(run.flow, run)
        node = graph.nodes[ex.node_id]
        taken = self._taken_edges(graph, node, result)
        now = self.clock()

        for e in taken:
            if e.loop and tx.loop_count(run.id, e.id) >= (e.max_loops or 0):
                self._loop_exhausted(tx, run, graph, ex, e, result, fence)
                return

        ref = self._store_result(tx, ex, node, result)
        extra = {"detail": {**ex.detail, **detail}} if detail else {}
        self._transition(
            tx,
            ex,
            E.DONE,
            fence,
            result_ref=ref,
            finished_at=now,
            lease_owner=None,
            error=None,
            reason=None,
            **extra,
        )
        for e in graph.out_edges(node.id):
            if e in taken:
                self._send(tx, run, graph, e, live=True)
            elif not e.loop:
                self._send(tx, run, graph, e, live=False)
        log.info("%s done", ex.key, extra=_log_ctx(ex))
        self.refresh_run(tx, run.id)

    def fail(
        self, tx: LedgerTx, ex: Execution, error: str, *, retryable: bool, fence: Fence
    ) -> ExecStatus:
        error = self.redactor.redact(error)
        run = self._run(tx, ex.run_id)
        node = self.graphs(run.flow, run).nodes[ex.node_id]
        now = self.clock()
        if retryable and ex.attempt < node.max_attempts:
            delay = self.backoff(ex.attempt)
            self._transition(
                tx,
                ex,
                E.FAILED,
                fence,
                attempt=ex.attempt + 1,
                error=error,
                next_attempt_at=now + delay,
                lease_owner=None,
            )
            status = E.FAILED
        else:
            self._transition(tx, ex, E.DEAD, fence, error=error, finished_at=now, lease_owner=None)
            status = E.DEAD
        log.warning("%s %s: %s", ex.key, status, error, extra=_log_ctx(ex))
        self.refresh_run(tx, run.id)
        return status

    def needs_review(
        self,
        tx: LedgerTx,
        ex: Execution,
        reason: str,
        detail: dict[str, Any],
        fence: Fence,
        error: str | None = None,
    ) -> None:
        detail = self.redactor.redact_value(detail)
        if ex.detail.get("retries"):  # keep the manual-retry count (see retry_run)
            detail = {**detail, "retries": ex.detail["retries"]}
        error = self.redactor.redact_opt(error)
        self._transition(
            tx,
            ex,
            E.NEEDS_REVIEW,
            fence,
            reason=reason,
            detail=detail,
            error=error,
            lease_owner=None,
        )
        log.warning("%s needs review: %s", ex.key, reason, extra=_log_ctx(ex))
        self.refresh_run(tx, ex.run_id)

    def park(
        self,
        tx: LedgerTx,
        ex: Execution,
        correlation_key: str,
        detail: dict[str, Any],
        fence: Fence,
    ) -> bool:
        """Wait for an event. Returns True if a matching event had already arrived (completed)."""
        detail = self.redactor.redact_value(detail)
        if detail.get("resume"):
            # everything that arrived while the step ran wakes it once (it reads what came)
            arrived = False
            while (event := tx.unconsumed_event(correlation_key, since=ex.created_at)) is not None:
                tx.consume_event(event.id, ex.id)
                arrived = True
            if arrived:
                self._transition(tx, ex, E.READY, fence, detail=detail, lease_owner=None)
                self.refresh_run(tx, ex.run_id)
                return True
        event = tx.unconsumed_event(correlation_key, since=ex.created_at)
        if event is not None:
            tx.consume_event(event.id, ex.id)
            self.complete(tx, ex, event.payload, fence)
            return True
        self._transition(
            tx,
            ex,
            E.WAITING,
            fence,
            correlation_key=correlation_key,
            detail=detail,
            lease_owner=None,
        )
        self.refresh_run(tx, ex.run_id)
        return False

    def reject(self, tx: LedgerTx, exec_id: str, error: str) -> None:
        """A human rejected a gate: the run goes dead (it can be retried)."""
        error = self.redactor.redact(error)
        ex = tx.get_execution(exec_id)
        if ex is None or ex.status != E.WAITING:
            raise ValidationError(f"execution {exec_id} is not waiting")
        self._transition(tx, ex, E.DEAD, Fence(E.WAITING), error=error, finished_at=self.clock())
        self.refresh_run(tx, ex.run_id)

    def heartbeat(self, tx: LedgerTx, ex: Execution, lease_ttl: float) -> bool:
        return tx.update_execution(
            ex.id,
            expect_status=E.RUNNING,
            expect_epoch=ex.lease_epoch,
            lease_expires=self.clock() + lease_ttl,
        )

    def reclaim_expired(self, tx: LedgerTx) -> list[str]:
        """Put executions whose worker stopped heartbeating back to ready.
        Crashes don't use up `max_attempts`; a separate cap stops crash loops."""
        reclaimed = []
        for ex in tx.expired_leases(self.clock()):
            fence = Fence(E.RUNNING, ex.lease_epoch)
            if ex.reclaims + 1 > self.max_reclaims:
                self._transition(
                    tx,
                    ex,
                    E.DEAD,
                    fence,
                    error=f"lease expired {ex.reclaims + 1} times (worker crash loop?)",
                    lease_owner=None,
                )
            else:
                self._transition(tx, ex, E.READY, fence, reclaims=ex.reclaims + 1, lease_owner=None)
            reclaimed.append(ex.id)
            self.refresh_run(tx, ex.run_id)
        return reclaimed

    # -- events, human actions -------------------------------------------------------------
    def deliver_event(self, tx: LedgerTx, correlation_key: str, payload: Any) -> list[str]:
        """Record an event and complete every execution waiting on its key."""
        payload = self.redactor.redact_value(payload)
        event_id = tx.insert_event(correlation_key, payload, self.clock())
        woken = []
        for ex in tx.waiting_by_key(correlation_key):
            if ex.detail.get("resume"):
                self._transition(tx, ex, E.READY, Fence(E.WAITING), lease_owner=None)
                self.refresh_run(tx, ex.run_id)
            else:
                self.complete(tx, ex, payload, Fence(E.WAITING))
            woken.append(ex.id)
        if woken:
            tx.consume_event(event_id, woken[0])
        return woken

    def resolve(
        self,
        tx: LedgerTx,
        exec_id: str,
        action: str,
        *,
        result: Any = None,
        effect_decision: str | None = None,
        effect_result: Any = None,
    ) -> None:
        """Human decision on a needs-review execution: retry | skip | fail | done."""
        ex = tx.get_execution(exec_id)
        if ex is None:
            raise ValidationError(f"no execution {exec_id}")
        if ex.status != E.NEEDS_REVIEW:
            raise ValidationError(f"execution {ex.key} is {ex.status}, not needs-review")
        fence = Fence(E.NEEDS_REVIEW)
        run = self._run(tx, ex.run_id)
        if effect_decision is not None:
            self._decide_effects(tx, ex, effect_decision, effect_result)
        if action == "retry":
            retries = int(ex.detail.get("retries") or 0) + 1  # a fresh effect namespace
            self._transition(
                tx, ex, E.READY, fence, next_attempt_at=0.0, error=None, reason=None,
                detail={"retries": retries},
            )  # fmt: skip
        elif action == "skip":
            self._transition(tx, ex, E.SKIPPED, fence, finished_at=self.clock())
            graph = self.graphs(run.flow, run)
            for e in graph.out_edges(ex.node_id):
                if not e.loop:
                    self._send(tx, run, graph, e, live=False)
        elif action == "fail":
            self._transition(tx, ex, E.DEAD, fence, finished_at=self.clock())
        elif action == "done":
            self.complete(tx, ex, result if result is not None else {}, fence)
            return
        else:
            raise ValidationError(f"unknown resolve action {action!r}")
        self.refresh_run(tx, run.id)

    def _decide_effects(
        self, tx: LedgerTx, ex: Execution, decision: str, effect_result: Any
    ) -> None:
        for eff in tx.list_effects(ex.run_id):
            if (eff.node_id, eff.iteration) != (ex.node_id, ex.iteration) or eff.status not in (
                "intent",
                "failed",
            ):
                continue
            if decision == "redo":
                eff.status = "redo"
            elif decision == "done":
                eff.status, eff.result = "committed", self.redactor.redact_value(effect_result)
            else:
                raise ValidationError(f"effect decision must be redo or done, not {decision!r}")
            eff.updated_at = self.clock()
            tx.put_effect(eff)

    def claim(self, tx: LedgerTx, owner: str, lease_ttl: float) -> Execution | None:
        """Claim the next ready (or backed-off) execution, journaled with the claim."""
        ex = tx.claim_next(owner, self.clock(), lease_ttl)
        if ex is not None:
            tx.add_journal(ex.run_id, "status", "claimed -> running", execution_id=ex.id,
                           node_id=ex.node_id, iteration=ex.iteration,
                           detail={"attempt": ex.attempt, "lease_owner": owner},
                           at=self.clock())  # fmt: skip
        return ex

    def cancel(self, tx: LedgerTx, run_id: str) -> None:
        for ex in tx.list_executions(run_id):
            if ex.status not in TERMINAL_EXEC:
                tx.update_execution(ex.id, status=E.CANCELLED, lease_owner=None)
                tx.add_journal(run_id, "status", f"{ex.status} -> {E.CANCELLED}",
                               execution_id=ex.id, node_id=ex.node_id, iteration=ex.iteration,
                               at=self.clock())  # fmt: skip
        tx.update_run(run_id, status=RunStatus.CANCELLED)

    def retry_run(self, tx: LedgerTx, run_id: str, node_id: str | None = None) -> int:
        """Give dead executions (of one node, or all) a fresh set of attempts; a needs-review
        one of that node is resolved with retry. A for_each node keeps its finished tasks."""
        n = 0
        for ex in tx.list_executions(run_id):
            if node_id is not None and ex.node_id != node_id:
                continue
            if node_id is not None and ex.status == E.NEEDS_REVIEW:
                self.resolve(tx, ex.id, "retry")
                n += 1
                continue
            if ex.status == E.DEAD:
                self._transition(
                    tx,
                    ex,
                    E.READY,
                    Fence(E.DEAD),
                    attempt=1,
                    reclaims=0,
                    next_attempt_at=0.0,
                    error=None,
                    # counts manual retries: attempt-named effects (shell.run's `run#<n>`) must
                    # not reuse the result recorded before the retry
                    detail={**ex.detail, "retries": int(ex.detail.get("retries") or 0) + 1},
                )
                n += 1
        self.refresh_run(tx, run_id)
        return n

    # -- inputs -----------------------------------------------------------------------------
    def resolve_inputs(self, tx: LedgerTx, ex: Execution) -> dict[str, Any]:
        """The node's declared inputs (only those): latest done output of each producing node.

        Each one loaded is recorded as a `read` lineage link. Inputs larger than `handoff_max`
        are passed as an artifact reference dict (`ArtifactRef.as_dict()`) instead of a value.
        """
        run = self._run(tx, ex.run_id)
        graph = self.graphs(run.flow, run)
        node = graph.nodes[ex.node_id]
        inputs: dict[str, Any] = {"payload": run.payload}
        done = [x for x in tx.list_executions(run.id) if x.status == E.DONE and x.result_ref]
        for name in node.inputs:
            artifact, optional = parse_input(name)
            if artifact == "payload":
                continue
            producers = set(graph.producers(artifact))
            candidates = [x for x in done if x.node_id in producers]
            if not candidates:
                if optional:
                    continue
                raise PermanentError(f"input {artifact!r} for {ex.key} was never produced")
            latest = max(candidates, key=lambda x: (x.finished_at or 0, x.iteration))
            digest = latest.result_ref or ""
            meta = self.artifacts.meta(tx, digest)
            if meta is not None and meta.size > self.handoff_max:
                inputs[artifact] = self.artifacts.ref(tx, meta).as_dict()
            else:
                inputs[artifact] = self.artifacts.load(tx, digest)
            self._link(tx, ex, artifact, digest, "read", source=latest.id)
        return inputs

    def read_result(self, tx: LedgerTx, ex: Execution) -> Any:
        if not ex.result_ref:
            return None
        return self.artifacts.load(tx, ex.result_ref)

    def _store_result(self, tx: LedgerTx, ex: Execution, node: NodeDef, result: Any) -> str:
        art = self.artifacts.put_json(tx, result)
        self._link(tx, ex, node.out_name, art.hash, "wrote")
        return art.hash

    def _link(
        self,
        tx: LedgerTx,
        ex: Execution,
        name: str,
        digest: str,
        direction: str,
        source: str | None = None,
    ) -> None:
        tx.add_artifact_link(
            ArtifactLink(
                run_id=ex.run_id,
                execution_id=ex.id,
                node_id=ex.node_id,
                iteration=ex.iteration,
                name=name,
                hash=digest,
                direction=direction,
                at=self.clock(),
                source_execution_id=source,
            )
        )

    # -- internals --------------------------------------------------------------------------
    def backoff(self, attempt: int) -> float:
        base = min(self.backoff_max, self.backoff_base * 2 ** (attempt - 1))
        return base * random.uniform(0.8, 1.2)

    def refresh_run(self, tx: LedgerTx, run_id: str) -> RunStatus:
        run = self._run(tx, run_id)
        if run.status == RunStatus.CANCELLED:
            return RunStatus.CANCELLED
        execs = tx.list_executions(run_id)
        statuses = [ExecStatus(x.status) for x in execs]
        started = any(x.status not in (E.PENDING, E.READY) or x.attempt > 1 for x in execs)
        status = derive_run_status(statuses, started)
        if status != run.status:
            error = None
            if status == RunStatus.DEAD:
                dead = [x for x in execs if x.status == E.DEAD]
                error = "; ".join(f"{x.key}: {x.error}" for x in dead)
            tx.update_run(run_id, status=status, error=error)
            log.info("run %s: %s -> %s", run_id, run.status, status, extra={"run_id": run_id})
        return status

    def _run(self, tx: LedgerTx, run_id: str) -> Run:
        run = tx.get_run(run_id)
        if run is None:
            raise ValidationError(f"no run {run_id}")
        return run

    def _insert(self, tx: LedgerTx, ex: Execution) -> None:
        tx.insert_execution(ex)
        tx.add_journal(
            ex.run_id,
            "status",
            f"created ({ex.status})",
            execution_id=ex.id,
            node_id=ex.node_id,
            iteration=ex.iteration,
            at=self.clock(),
        )

    def _transition(
        self, tx: LedgerTx, ex: Execution, dst: ExecStatus, fence: Fence, **fields: Any
    ) -> None:
        check_exec_transition(fence.status, dst)
        ok = tx.update_execution(
            ex.id, expect_status=fence.status, expect_epoch=fence.epoch, status=dst, **fields
        )
        if not ok:
            raise StaleLease(f"{ex.key}: expected {fence.status} epoch {fence.epoch}")
        # the journal: every status change, with what explains it
        detail = {
            k: fields[k] for k in ("attempt", "lease_owner", "next_attempt_at") if k in fields
        }
        if fields.get("error"):
            detail["error"] = str(fields["error"])[:2000]
        tx.add_journal(
            ex.run_id,
            "status",
            f"{fence.status} -> {dst}",
            execution_id=ex.id,
            node_id=ex.node_id,
            iteration=ex.iteration,
            detail=detail,
            at=self.clock(),
        )

    def _taken_edges(self, graph: Graph, node: NodeDef, result: Any) -> list[EdgeDef]:
        out = graph.out_edges(node.id)
        if node.kind != "router":
            return out
        label = result.get("route") if isinstance(result, dict) else None
        if label is None:
            raise PermanentError(f"router {node.id!r} returned no 'route'")
        matched = [e for e in out if e.when == str(label)]
        if not matched:
            matched = [e for e in out if e.when == DEFAULT_WHEN]
        if not matched:
            choices = ", ".join(str(e.when) for e in out)
            raise PermanentError(f"router {node.id!r}: no edge for {label!r} (have: {choices})")
        return matched

    def _loop_exhausted(
        self,
        tx: LedgerTx,
        run: Run,
        graph: Graph,
        ex: Execution,
        edge: EdgeDef,
        result: Any,
        fence: Fence,
    ) -> None:
        detail = {"edge": edge.id, "max_loops": edge.max_loops}
        if edge.on_exhausted == "needs-review":
            self.needs_review(tx, ex, "loop_exhausted", detail, fence)
        elif edge.on_exhausted == "fail":
            self._transition(
                tx, ex, E.DEAD, fence, error=f"loop {edge.id} exhausted", lease_owner=None
            )
            self.refresh_run(tx, run.id)
        else:  # continue at another node
            ref = self._store_result(tx, ex, graph.nodes[ex.node_id], result)
            self._transition(
                tx,
                ex,
                E.DONE,
                fence,
                result_ref=ref,
                finished_at=self.clock(),
                lease_owner=None,
                detail=detail,
            )
            target = EdgeDef(ex.node_id, edge.on_exhausted)
            self._send(tx, run, graph, target, live=True, tokens_needed=1)
            self.refresh_run(tx, run.id)

    def _send(
        self,
        tx: LedgerTx,
        run: Run,
        graph: Graph,
        edge: EdgeDef,
        *,
        live: bool,
        tokens_needed: int | None = None,
    ) -> None:
        now = self.clock()
        if edge.loop:
            tx.inc_loop_count(run.id, edge.id)
            self._insert(
                tx,
                Execution(
                    id=new_id("x"),
                    run_id=run.id,
                    node_id=edge.dst,
                    iteration=tx.max_iteration(run.id, edge.dst) + 1,
                    status=E.READY,
                    tokens_needed=1,
                    tokens_live=1,
                    created_at=now,
                ),
            )
            return
        ex = tx.open_execution(run.id, edge.dst)
        if ex is None:
            needed = tokens_needed if tokens_needed is not None else len(graph.in_edges(edge.dst))
            ex = Execution(
                id=new_id("x"),
                run_id=run.id,
                node_id=edge.dst,
                iteration=tx.max_iteration(run.id, edge.dst) + 1,
                status=E.PENDING,
                tokens_needed=needed,
                created_at=now,
            )
            self._insert(tx, ex)
        live_n = ex.tokens_live + (1 if live else 0)
        skip_n = ex.tokens_skip + (0 if live else 1)
        if live_n + skip_n < ex.tokens_needed:
            tx.update_execution(ex.id, tokens_live=live_n, tokens_skip=skip_n)
            return
        fence = Fence(E.PENDING)
        if live_n > 0:
            self._transition(tx, ex, E.READY, fence, tokens_live=live_n, tokens_skip=skip_n)
            return
        self._transition(
            tx, ex, E.SKIPPED, fence, tokens_live=live_n, tokens_skip=skip_n, finished_at=now
        )
        for e in graph.out_edges(edge.dst):
            if not e.loop:
                self._send(tx, run, graph, e, live=False)


def _log_ctx(ex: Execution) -> dict[str, Any]:
    return {
        "run_id": ex.run_id,
        "node_id": ex.node_id,
        "iteration": ex.iteration,
        "execution_id": ex.id,
    }
