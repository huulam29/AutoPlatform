"""Effect guard, budgets, leases."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentline import Advice, AdviceRequest, App, BudgetExceeded, RunStatus, StepContext

from .test_engine import drain


class FakeGitHub:
    def __init__(self) -> None:
        self.prs: dict[str, list[int]] = {}

    async def create(self, branch: str) -> dict[str, Any]:
        n = sum(len(v) for v in self.prs.values()) + 1
        self.prs.setdefault(branch, []).append(n)
        return {"number": n}

    async def find(self, branch: str) -> dict[str, Any] | None:
        nums = self.prs.get(branch)
        return {"number": nums[0]} if nums else None


def simulate_crash_after_effect(app: App, run_id: str) -> None:
    """Make the next attempt look like the previous worker died between do() and commit:
    leave the effect in 'intent' and put the execution back to ready."""
    with app.ledger.tx() as tx:
        for eff in tx.list_effects(run_id):
            eff.status = "intent"
            tx.put_effect(eff)


def test_committed_effect_is_not_repeated_on_retry(app: App) -> None:
    gh = FakeGitHub()
    flow = app.flow("t")

    @flow.step("open_pr", max_attempts=3)
    async def open_pr(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        pr = await ctx.effect("create_pr", lambda: gh.create("b1"))
        if ctx.attempt == 1:
            raise RuntimeError("crash after effect committed")
        return {"pr": pr}

    sub = app.submit_sync("t", {})
    drain(app)
    assert gh.prs == {"b1": [1]}
    assert app.result(sub.run_id, "open_pr") == {"pr": {"number": 1}}


@pytest.mark.parametrize("found", [True, False])
def test_unknown_effect_resolved_by_check(app: App, found: bool) -> None:
    gh = FakeGitHub()
    flow = app.flow("t")

    @flow.step("open_pr", max_attempts=3)
    async def open_pr(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        async def do() -> dict[str, Any]:
            if ctx.attempt == 1 and not found:
                raise ConnectionError("request never reached GitHub")
            return await gh.create("b1")

        pr = await ctx.effect("create_pr", do, check=lambda: gh.find("b1"))
        if ctx.attempt == 1:
            raise RuntimeError("crash before commit")
        return {"pr": pr}

    sub = app.submit_sync("t", {})
    with app.ledger.tx() as tx:  # attempt 1 by hand so the effect stays at "intent"
        ex = tx.claim_next("w", app.scheduler.clock(), 30)
    assert ex is not None

    async def first_attempt() -> None:
        from agentline.context import StepContext as Ctx

        ctx = Ctx(app=app, execution=ex, payload={}, worker_id="w")
        with pytest.raises((RuntimeError, ConnectionError)):
            await open_pr(ctx, {})

    asyncio.run(first_attempt())
    simulate_crash_after_effect(app, sub.run_id)
    with app.ledger.tx() as tx:
        tx.update_execution(ex.id, status="ready", attempt=2)
    drain(app)
    assert gh.prs == {"b1": [1]}
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]


def _unknown_effect_flow(app: App, on_unknown: str, calls: list[int]) -> None:
    flow = app.flow("t")

    @flow.step("comment", max_attempts=3)
    async def comment(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        async def do() -> dict[str, Any]:
            calls.append(ctx.attempt)
            return {"id": len(calls)}

        out = await ctx.effect(
            "post_comment",
            do,
            on_unknown=on_unknown,  # type: ignore[arg-type]
            description="jira comment",
        )
        if ctx.attempt == 1:
            # Pretend we crashed between do() and commit on attempt 1.
            with app.ledger.tx() as tx:
                eff = tx.get_effect(ctx.run_id, ctx.node_id, ctx.iteration, "post_comment")
                assert eff is not None
                eff.status = "intent"
                tx.put_effect(eff)
            raise RuntimeError("crash")
        return out


def test_unknown_effect_halts_by_default_then_human_redoes(app: App) -> None:
    calls: list[int] = []
    _unknown_effect_flow(app, "halt", calls)
    sub = app.submit_sync("t", {})
    drain(app)
    (ex,) = app.executions(sub.run_id)
    assert ex.status == "needs-review" and ex.reason == "effect_unknown"
    assert calls == [1]
    asyncio.run(app.resolve(ex.id, "retry", effect_decision="redo"))
    drain(app)
    assert calls == [1, 2]  # resolve-retry reuses attempt 2
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]


def test_unknown_effect_human_marks_it_done(app: App) -> None:
    calls: list[int] = []
    _unknown_effect_flow(app, "halt", calls)
    sub = app.submit_sync("t", {})
    drain(app)
    (ex,) = app.executions(sub.run_id)
    asyncio.run(app.resolve(ex.id, "retry", effect_decision="done", effect_result={"id": 1}))
    drain(app)
    assert calls == [1]
    assert app.result(sub.run_id, "comment") == {"id": 1}


def test_unknown_effect_retry_policy_redoes(app: App) -> None:
    calls: list[int] = []
    _unknown_effect_flow(app, "retry", calls)
    sub = app.submit_sync("t", {})
    drain(app)
    assert calls == [1, 2]
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]


@pytest.mark.parametrize(("decision", "expected"), [("retry", "done"), ("human", "needs-review")])
def test_unknown_effect_ai_advisor(tmp_path: Any, decision: str, expected: str) -> None:
    from agentline import SqliteLedger

    asked: list[AdviceRequest] = []

    async def advisor(req: AdviceRequest) -> Advice:
        asked.append(req)
        return Advice(decision, "comments are harmless to repeat", cost_usd=0.01)  # type: ignore[arg-type]

    app = App(SqliteLedger(tmp_path / "l.db"), advisor=advisor, backoff_base=0)
    calls: list[int] = []
    _unknown_effect_flow(app, "ai", calls)
    sub = app.submit_sync("t", {})
    drain(app)
    assert asked and asked[0].effect == "post_comment"
    assert app.get_run(sub.run_id).status == expected  # type: ignore[union-attr]
    run = app.get_run(sub.run_id)
    assert run is not None and run.cost_usd == pytest.approx(0.01)
    with app.ledger.tx() as tx:
        (eff,) = tx.list_effects(sub.run_id)
    assert eff.advice == {"decision": decision, "reason": "comments are harmless to repeat"}


def test_ai_on_unknown_is_rejected_for_deploy_class(app: App) -> None:
    flow = app.flow("t")

    @flow.step("deploy", max_attempts=1)
    async def deploy(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        async def do() -> None:
            return None

        return await ctx.effect("deploy", do, on_unknown="ai", deploy_class=True)

    sub = app.submit_sync("t", {})
    drain(app)
    (ex,) = app.executions(sub.run_id)
    assert ex.status == "dead" and "not allowed on deploy-class" in (ex.error or "")


def test_budget_exceeded_goes_to_needs_review(app: App) -> None:
    flow = app.flow("t", budget_usd=0.05)

    @flow.step("agent")
    async def agent(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        for _ in range(10):
            await ctx.check_budget(0.02)
            await ctx.spend(0.02, "llm")
        return {}

    sub = app.submit_sync("t", {})
    drain(app)
    run = app.get_run(sub.run_id)
    assert run is not None and run.status == RunStatus.NEEDS_REVIEW
    (ex,) = app.executions(sub.run_id)
    assert ex.reason == "budget_exceeded"
    assert run.cost_usd == pytest.approx(0.04)  # stopped by check_budget before a 3rd call
    assert ex.detail["scope"] == "run"


def test_node_budget(app: App) -> None:
    flow = app.flow("t")

    @flow.step("agent", budget_usd=0.01)
    async def agent(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        await ctx.spend(0.02)
        return {}

    sub = app.submit_sync("t", {})
    drain(app)
    (ex,) = app.executions(sub.run_id)
    assert ex.reason == "budget_exceeded" and ex.detail["scope"] == "node"
    assert isinstance(BudgetExceeded(1, 0), Exception)


def test_expired_lease_is_reclaimed_without_using_an_attempt(app: App) -> None:
    flow = app.flow("t")

    @flow.step("a", max_attempts=1)
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"attempt": ctx.attempt}

    sub = app.submit_sync("t", {})
    with app.ledger.tx() as tx:  # a worker claims it and then dies
        tx.claim_next("dead-worker", app.scheduler.clock(), lease_ttl=-1)
    drain(app)
    (ex,) = app.executions(sub.run_id)
    assert ex.status == "done" and ex.reclaims == 1 and ex.attempt == 1


def test_crash_loop_cap(tmp_path: Any) -> None:
    from agentline import SqliteLedger

    app = App(SqliteLedger(tmp_path / "l.db"), max_reclaims=2)
    flow = app.flow("t")

    @flow.step("a")
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {}

    sub = app.submit_sync("t", {})
    for _ in range(3):
        with app.ledger.tx() as tx:
            tx.claim_next("w", app.scheduler.clock(), lease_ttl=-1)
            app.scheduler.reclaim_expired(tx)
    (ex,) = app.executions(sub.run_id)
    assert ex.status == "dead" and "crash loop" in (ex.error or "")


def test_heartbeat_keeps_long_step_from_being_reclaimed(app: App) -> None:
    flow = app.flow("t")
    calls: list[int] = []

    @flow.step("long")
    async def long(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        await asyncio.sleep(0.6)
        return {}

    sub = app.submit_sync("t", {})

    async def two_workers() -> None:
        w1 = app.worker(concurrency=1, lease_ttl=0.15, worker_id="w1")
        w2 = app.worker(concurrency=1, lease_ttl=0.15, worker_id="w2")
        await asyncio.gather(w1.run_until_idle(), w2.run_until_idle())

    asyncio.run(two_workers())
    assert calls == [1]
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]


def test_lost_lease_cancels_handler_and_its_commit_is_rejected(app: App) -> None:
    flow = app.flow("t")
    finished: list[int] = []

    @flow.step("a")
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        if ctx.attempt == 1 and ctx.execution.reclaims == 0:
            # Someone else reclaims and re-claims this execution (bumps the epoch).
            with app.ledger.tx() as tx:
                tx.update_execution(ctx.execution.id, lease_epoch=ctx.execution.lease_epoch + 5)
            await asyncio.sleep(1)
        finished.append(ctx.execution.lease_epoch)
        return {}

    app.submit_sync("t", {})

    async def go() -> None:
        w = app.worker(concurrency=1, lease_ttl=0.15)
        await asyncio.wait_for(w.tick(), 5)

    asyncio.run(go())
    assert finished == []  # the stale handler was cancelled before finishing
