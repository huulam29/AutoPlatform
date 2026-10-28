"""What a step handler gets: its identity, budget, logger, and the effect guard."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from agentline.artifacts import ArtifactRef, guess_media_type, is_ref
from agentline.errors import BudgetExceeded, NeedsReview, StaleLease
from agentline.ledger.base import ArtifactLink, Effect, Execution
from agentline.results import Route, Wait
from agentline.states import ExecStatus

if TYPE_CHECKING:
    from agentline.app import App

T = TypeVar("T")
OnUnknown = Literal["halt", "retry", "ai"]


@dataclass(frozen=True)
class AdviceRequest:
    run_id: str
    node_id: str
    iteration: int
    effect: str
    description: str
    has_check: bool
    has_key: bool


@dataclass(frozen=True)
class Advice:
    decision: Literal["retry", "halt", "human"]
    reason: str
    cost_usd: float = 0.0


Advisor = Callable[[AdviceRequest], Awaitable[Advice]]


@dataclass
class StepContext:
    app: App
    execution: Execution
    payload: Any
    worker_id: str
    node_budget_usd: float | None = None
    notes: dict[str, Any] = field(default_factory=dict)  # merged into the execution's detail
    # a for_each task's copy of the context namespaces its effects ("task3:send")
    effect_prefix: str = ""

    def __post_init__(self) -> None:
        self.log = logging.LoggerAdapter(
            logging.getLogger("agentline.step"),
            {"run_id": self.run_id, "node_id": self.node_id, "iteration": self.iteration},
        )

    @property
    def run_id(self) -> str:
        return self.execution.run_id

    @property
    def node_id(self) -> str:
        return self.execution.node_id

    @property
    def iteration(self) -> int:
        return self.execution.iteration

    @property
    def attempt(self) -> int:
        return self.execution.attempt

    @property
    def retries(self) -> int:
        """Manual retries of this execution (`agentline runs retry`, the Retry button)."""
        return int(self.execution.detail.get("retries") or 0)

    @property
    def attempt_key(self) -> str:
        """Unique per try, across manual retries: `3` on first runs, `r1.3` after one retry."""
        return f"r{self.retries}.{self.attempt}" if self.retries else str(self.attempt)

    # -- results ----------------------------------------------------------------------------
    def wait(self, correlation_key: str, *, resume: bool = False, **detail: Any) -> Wait:
        """Park on a key. `resume=True`: the event wakes the step to run again (it reads what
        arrived itself) instead of becoming its result."""
        return Wait(correlation_key, {**detail, "resume": True} if resume else detail)

    def route(self, label: str, **output: Any) -> Route:
        return Route(label, output)

    # -- notes and attachments -------------------------------------------------------------
    async def attach(self, name: str, data: str | bytes, media_type: str | None = None) -> str:
        """Store a large output (transcript, diff) as a file artifact, link it as written by
        this execution, and note it in the execution's `detail.attachments`. Returns its hash.
        The media type defaults to `text/x-diff` for `diff`, else `text/plain` for text."""
        raw = data.encode() if isinstance(data, str) else data
        mt = media_type or guess_media_type(name, data)
        raw = self.app.redactor.redact_bytes(raw)  # transcripts and diffs can echo secrets
        store = self.app.scheduler.artifacts
        ex = self.execution

        def _do() -> tuple[str, str]:
            with self.app.ledger.tx() as tx:
                art = store.put_bytes(tx, raw, mt)
                tx.add_artifact_link(
                    ArtifactLink(
                        run_id=ex.run_id,
                        execution_id=ex.id,
                        node_id=ex.node_id,
                        iteration=ex.iteration,
                        name=name,
                        hash=art.hash,
                        direction="wrote",
                        at=time.time(),
                    )
                )
                return art.hash, art.summary

        ref, summary = await asyncio.to_thread(_do)
        self.notes.setdefault("attachments", {})[name] = {
            "ref": ref,
            "size": len(raw),
            "summary": summary,
        }
        return ref

    async def load(self, ref: Any) -> Any:
        """Full content of an artifact reference: a JSON value, or bytes for files.
        Values that are not references are returned unchanged, so handlers can call
        `await ctx.load(inputs["plan"])` whether or not the input was large."""
        if not (isinstance(ref, ArtifactRef) or is_ref(ref)):
            return ref
        digest = ArtifactRef.from_value(ref).hash
        store = self.app.scheduler.artifacts

        def _do() -> Any:
            with self.app.ledger.tx() as tx:
                return store.load(tx, digest)

        return await asyncio.to_thread(_do)

    # -- audit ------------------------------------------------------------------------------
    async def audit(self, action: str, detail: dict[str, Any], *, actor: str | None = None) -> int:
        """Append an audit row for this run and node (e.g. `deploy`). Actor defaults to
        `agentline:<worker id>`."""
        who = actor or f"agentline:{self.worker_id}"
        clean = self.app.redactor.redact_value(detail)

        def _do() -> int:
            with self.app.ledger.tx() as tx:
                return tx.add_audit(who, action, self.run_id, self.node_id, clean)

        return await asyncio.to_thread(_do)

    # -- budget -----------------------------------------------------------------------------
    async def spend(self, usd: float, note: str = "") -> float:
        """Record a cost immediately (it's real even if this attempt crashes later), then
        raise BudgetExceeded if the run or node budget is now over."""

        def _do() -> tuple[float, float, float | None]:
            with self.app.ledger.tx() as tx:
                total = tx.add_cost(
                    self.run_id, self.execution.id, self.attempt, usd, note or self.node_id
                )
                run = tx.get_run(self.run_id)
                ex = tx.get_execution(self.execution.id)
                assert run is not None and ex is not None
                return total, ex.cost_usd, run.budget_usd

        total, node_total, budget = await asyncio.to_thread(_do)
        if budget is not None and total > budget + 1e-9:
            raise BudgetExceeded(total, budget, "run")
        if self.node_budget_usd is not None and node_total > self.node_budget_usd + 1e-9:
            raise BudgetExceeded(node_total, self.node_budget_usd, "node")
        return total

    async def remaining_budget(self) -> float | None:
        """Smallest of what's left in the run and node budgets (None = unlimited)."""

        def _do() -> tuple[float, float | None, float]:
            with self.app.ledger.tx() as tx:
                run = tx.get_run(self.run_id)
                ex = tx.get_execution(self.execution.id)
                assert run is not None and ex is not None
                return run.cost_usd, run.budget_usd, ex.cost_usd

        spent, budget, node_spent = await asyncio.to_thread(_do)
        left = [
            b
            for b in (
                None if budget is None else budget - spent,
                None if self.node_budget_usd is None else self.node_budget_usd - node_spent,
            )
            if b is not None
        ]
        return max(0.0, min(left)) if left else None

    async def check_budget(self, estimate_usd: float = 0.0) -> None:
        """Call before a paid operation with its estimated max cost."""

        def _do() -> tuple[float, float | None, float]:
            with self.app.ledger.tx() as tx:
                run = tx.get_run(self.run_id)
                ex = tx.get_execution(self.execution.id)
                assert run is not None and ex is not None
                return run.cost_usd, run.budget_usd, ex.cost_usd

        spent, budget, node_spent = await asyncio.to_thread(_do)
        if budget is not None and spent + estimate_usd > budget + 1e-9:
            raise BudgetExceeded(spent + estimate_usd, budget, "run")
        if self.node_budget_usd is not None and node_spent + estimate_usd > self.node_budget_usd:
            raise BudgetExceeded(node_spent + estimate_usd, self.node_budget_usd, "node")

    # -- effect guard -----------------------------------------------------------------------
    async def effect(
        self,
        name: str,
        do: Callable[[], Awaitable[T]],
        *,
        key: str | None = None,
        check: Callable[[], Awaitable[T | None]] | None = None,
        on_unknown: OnUnknown = "halt",
        deploy_class: bool = False,
        description: str = "",
    ) -> T:
        """Run an external side effect at most once per (run, node, iteration, name).

        intent is recorded before `do()`; committed after. If a previous attempt left an intent
        (it crashed around the call), `check()` decides whether it already happened; with only
        a `key`, the provider is trusted to dedupe; with neither, `on_unknown` applies.
        """
        if deploy_class and on_unknown == "ai":
            raise ValueError("on_unknown='ai' is not allowed on deploy-class effects")
        name = self.effect_prefix + name
        ident = (self.run_id, self.node_id, self.iteration, name)
        existing = await asyncio.to_thread(self._get_effect, ident)

        if existing is not None and existing.status == "committed":
            self.log.info("effect %s already committed, reusing result", name)
            return existing.result  # type: ignore[no-any-return]

        if existing is not None and existing.status == "intent":
            if check is not None:
                found = await check()
                if found is not None:
                    self.log.info("effect %s found by check after a crash", name)
                    await asyncio.to_thread(self._write_effect, ident, "committed", key, found)
                    return found
            elif key is None:
                await self._on_unknown(name, on_unknown, description, existing)

        await asyncio.to_thread(self._write_effect, ident, "intent", key, None)
        result = await do()
        await asyncio.to_thread(self._write_effect, ident, "committed", key, result)
        return result

    async def _on_unknown(
        self, name: str, policy: OnUnknown, description: str, existing: Effect
    ) -> None:
        detail: dict[str, Any] = {"effect": name, "description": description}
        if policy == "retry":
            self.log.warning("effect %s state unknown; on_unknown=retry, doing it again", name)
            return
        if policy == "ai":
            advisor = self.app.advisor
            if advisor is None:
                raise NeedsReview("effect_unknown", {**detail, "advice": "no advisor configured"})
            advice = await advisor(
                AdviceRequest(
                    self.run_id,
                    self.node_id,
                    self.iteration,
                    name,
                    description,
                    has_check=False,
                    has_key=False,
                )
            )
            if advice.cost_usd:
                await self.spend(advice.cost_usd, f"advisor:{name}")
            ident = (self.run_id, self.node_id, self.iteration, name)
            existing.advice = {"decision": advice.decision, "reason": advice.reason}
            await asyncio.to_thread(self._save_advice, ident, existing.advice)
            if advice.decision == "retry":
                self.log.warning("effect %s unknown; advisor says retry: %s", name, advice.reason)
                return
            raise NeedsReview("effect_unknown", {**detail, "advice": existing.advice})
        raise NeedsReview("effect_unknown", detail)

    def _get_effect(self, ident: tuple[str, str, int, str]) -> Effect | None:
        with self.app.ledger.tx() as tx:
            return tx.get_effect(*ident)

    def _save_advice(self, ident: tuple[str, str, int, str], advice: dict[str, Any]) -> None:
        with self.app.ledger.tx() as tx:
            eff = tx.get_effect(*ident)
            if eff is not None:
                eff.advice = advice
                tx.put_effect(eff)

    def _write_effect(
        self, ident: tuple[str, str, int, str], status: str, key: str | None, result: Any
    ) -> None:
        """Fenced: only the current lease holder may write effect records."""
        with self.app.ledger.tx() as tx:
            ex = tx.get_execution(self.execution.id)
            if (
                ex is None
                or ex.status != ExecStatus.RUNNING
                or ex.lease_epoch != self.execution.lease_epoch
            ):
                raise StaleLease(f"{self.execution.key}: lease lost before effect {ident[3]}")
            prev = tx.get_effect(*ident)
            tx.put_effect(
                Effect(
                    *ident,
                    status=status,
                    idempotency_key=key,
                    result=self.app.redactor.redact_value(result),
                    advice=prev.advice if prev else None,
                    updated_at=time.time(),
                )
            )
