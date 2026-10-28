"""Engine behavior through the Python API, with an in-process worker."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentline import App, NeedsReview, PermanentError, Route, RunStatus, StepContext
from agentline.errors import StaleLease
from agentline.scheduler import Fence

from .conftest import statuses


def drain(app: App, **kw: Any) -> None:
    asyncio.run(app.worker(concurrency=kw.pop("concurrency", 2), **kw).run_until_idle())


def test_linear_flow_passes_outputs(app: App) -> None:
    flow = app.flow("t")

    @flow.step("plan")
    async def plan(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"steps": [inputs["payload"]["ticket"], "test"]}

    @flow.step("implement", after="plan")
    async def implement(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"branch": f"agentline/{inputs['plan']['steps'][0]}"}

    sub = app.submit_sync("t", {"ticket": "T-1"})
    drain(app)
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]
    assert app.result(sub.run_id, "implement") == {"branch": "agentline/T-1"}


def test_dedupe_key_returns_existing_run(app: App) -> None:
    flow = app.flow("t")

    @flow.step("a")
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {}

    first = app.submit_sync("t", {}, dedupe_key="jira:T-1")
    second = app.submit_sync("t", {}, dedupe_key="jira:T-1")
    assert first.created and not second.created
    assert first.run_id == second.run_id


def test_transient_failure_is_retried(app: App) -> None:
    flow = app.flow("t")
    calls = []

    @flow.step("flaky", max_attempts=3)
    async def flaky(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        calls.append(ctx.attempt)
        if ctx.attempt < 3:
            raise RuntimeError("boom")
        return {"ok": True}

    sub = app.submit_sync("t", {})
    drain(app)
    assert calls == [1, 2, 3]
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]


def test_exhausted_attempts_go_dead_and_retry_revives(app: App) -> None:
    flow = app.flow("t")
    fail = {"on": True}

    @flow.step("a", max_attempts=2)
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        if fail["on"]:
            raise RuntimeError("nope")
        return {}

    sub = app.submit_sync("t", {})
    drain(app)
    run = app.get_run(sub.run_id)
    assert run is not None and run.status == RunStatus.DEAD
    assert "RuntimeError: nope" in (run.error or "")
    fail["on"] = False
    assert asyncio.run(app.retry(sub.run_id)) == 1
    drain(app)
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]


def test_permanent_error_is_not_retried(app: App) -> None:
    flow = app.flow("t")
    calls = []

    @flow.step("a", max_attempts=5)
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        raise PermanentError("bad config")

    sub = app.submit_sync("t", {})
    drain(app)
    assert len(calls) == 1
    assert app.get_run(sub.run_id).status == RunStatus.DEAD  # type: ignore[union-attr]


def test_timeout_counts_as_failed_attempt(app: App) -> None:
    flow = app.flow("t")

    @flow.step("slow", timeout=0.05, max_attempts=2)
    async def slow(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        if ctx.attempt == 1:
            await asyncio.sleep(1)
        return {"attempt": ctx.attempt}

    sub = app.submit_sync("t", {})
    drain(app)
    assert app.result(sub.run_id, "slow") == {"attempt": 2}


def test_parallel_branches_join(app: App) -> None:
    flow = app.flow("t")

    @flow.step("a")
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"n": 1}

    @flow.step("b", after="a")
    async def b(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"n": inputs["a"]["n"] + 1}

    @flow.step("c", after="a")
    async def c(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"n": inputs["a"]["n"] + 10}

    @flow.step("d", after=["b", "c"])
    async def d(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"sum": inputs["b"]["n"] + inputs["c"]["n"]}

    sub = app.submit_sync("t", {})
    drain(app, concurrency=3)
    assert app.result(sub.run_id, "d") == {"sum": 13}
    assert statuses(app, sub.run_id) == {"a#1": "done", "b#1": "done", "c#1": "done", "d#1": "done"}


def test_router_skips_untaken_branch_and_join_still_runs(app: App) -> None:
    flow = app.flow("t")

    @flow.router("r")
    async def r(ctx: StepContext, inputs: dict[str, Any]) -> Route:
        return ctx.route("left", why="test")

    @flow.step("left")
    async def left(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"side": "left"}

    @flow.step("right")
    async def right(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"side": "right"}

    @flow.step("join", after=["left", "right"], inputs=["left?", "right?"])
    async def join(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"got": sorted(k for k in inputs if k != "payload")}

    flow.edge("r", "left", when="left")
    flow.edge("r", "right", when="right")
    sub = app.submit_sync("t", {})
    drain(app)
    s = statuses(app, sub.run_id)
    assert s["right#1"] == "skipped" and s["join#1"] == "done"
    assert app.result(sub.run_id, "join") == {"got": ["left"]}
    assert app.result(sub.run_id, "r") == {"route": "left", "why": "test"}


def build_review_loop(app: App, verdicts: list[str], max_loops: int = 2, **edge: Any) -> None:
    flow = app.flow("loop")
    seen: list[int] = []

    @flow.step("implement", inputs=["review?"])
    async def implement(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        seen.append(ctx.iteration)
        return {"version": ctx.iteration, "had_review": "review" in inputs}

    @flow.step("review", after="implement")
    async def review(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"state": verdicts[inputs["implement"]["version"] - 1]}

    @flow.router("route", after="review")
    async def route(ctx: StepContext, inputs: dict[str, Any]) -> Route:
        return ctx.route(inputs["review"]["state"])

    @flow.step("deploy")
    async def deploy(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"deployed": True}

    flow.edge("route", "implement", when="changes_requested", max_loops=max_loops, **edge)
    flow.edge("route", "deploy", when="approved")
    app.seen = seen  # type: ignore[attr-defined]


def test_bounded_loop_runs_new_iterations(app: App) -> None:
    build_review_loop(app, ["changes_requested", "approved"])
    sub = app.submit_sync("loop", {})
    drain(app)
    s = statuses(app, sub.run_id)
    assert app.seen == [1, 2]  # type: ignore[attr-defined]
    assert s["implement#2"] == "done" and s["review#2"] == "done" and s["deploy#2"] == "done"
    assert s["deploy#1"] == "skipped"
    assert app.result(sub.run_id, "implement") == {"version": 2, "had_review": True}
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]


def test_loop_exhausted_goes_to_needs_review(app: App) -> None:
    build_review_loop(app, ["changes_requested"] * 5, max_loops=2)
    sub = app.submit_sync("loop", {})
    drain(app)
    run = app.get_run(sub.run_id)
    assert run is not None and run.status == RunStatus.NEEDS_REVIEW
    assert app.seen == [1, 2, 3]  # type: ignore[attr-defined]
    stuck = [x for x in app.executions(sub.run_id) if x.status == "needs-review"]
    assert [(x.key, x.reason) for x in stuck] == [("route#3", "loop_exhausted")]
    # A human decides to ship anyway.
    asyncio.run(app.resolve(stuck[0].id, "done", result={"route": "approved"}))
    drain(app)
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]


def test_loop_exhausted_fail(app: App) -> None:
    build_review_loop(app, ["changes_requested"] * 5, max_loops=1, on_exhausted="fail")
    sub = app.submit_sync("loop", {})
    drain(app)
    assert app.get_run(sub.run_id).status == RunStatus.DEAD  # type: ignore[union-attr]


def test_wait_parks_without_worker_and_resumes_on_event(app: App) -> None:
    flow = app.flow("t")

    @flow.step("gate")
    async def gate(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        return ctx.wait(f"gate:{ctx.run_id}:gate", prompt="Deploy?")

    @flow.step("deploy", after="gate")
    async def deploy(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {"approved_by": inputs["gate"]["by"]}

    sub = app.submit_sync("t", {})
    drain(app)
    assert app.get_run(sub.run_id).status == RunStatus.WAITING  # type: ignore[union-attr]
    woken = asyncio.run(app.deliver_event(f"gate:{sub.run_id}:gate", {"by": "huu"}))
    assert len(woken) == 1
    drain(app)
    assert app.result(sub.run_id, "deploy") == {"approved_by": "huu"}


def test_event_that_arrives_before_park_is_not_lost(app: App) -> None:
    flow = app.flow("t")

    @flow.step("wait")
    async def wait(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        await asyncio.to_thread(
            lambda: asyncio.run(app.deliver_event("pr:1", {"state": "approved"}))
        )
        return ctx.wait("pr:1")

    sub = app.submit_sync("t", {})
    drain(app)
    assert app.result(sub.run_id, "wait") == {"state": "approved"}


def test_cancel_run(app: App) -> None:
    flow = app.flow("t")

    @flow.step("gate")
    async def gate(ctx: StepContext, inputs: dict[str, Any]) -> Any:
        return ctx.wait("never")

    sub = app.submit_sync("t", {})
    drain(app)
    asyncio.run(app.cancel(sub.run_id))
    assert app.get_run(sub.run_id).status == RunStatus.CANCELLED  # type: ignore[union-attr]
    assert statuses(app, sub.run_id) == {"gate#1": "cancelled"}


def test_needs_review_raised_by_step(app: App) -> None:
    flow = app.flow("t")

    @flow.step("a")
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        raise NeedsReview("ambiguous_ticket", {"hint": "no acceptance criteria"})

    sub = app.submit_sync("t", {})
    drain(app)
    (ex,) = app.executions(sub.run_id)
    assert ex.status == "needs-review" and ex.detail == {"hint": "no acceptance criteria"}
    asyncio.run(app.resolve(ex.id, "skip"))
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]


def test_stale_epoch_cannot_commit(app: App) -> None:
    flow = app.flow("t")

    @flow.step("a")
    async def a(ctx: StepContext, inputs: dict[str, Any]) -> dict[str, Any]:
        return {}

    sub = app.submit_sync("t", {})
    with app.ledger.tx() as tx:
        ex = tx.claim_next("w1", 0.0 + app.scheduler.clock(), 30)
    assert ex is not None
    with pytest.raises(StaleLease), app.ledger.tx() as tx:
        app.scheduler.complete(tx, ex, {}, Fence("running", ex.lease_epoch - 1))
    with app.ledger.tx() as tx:
        app.scheduler.complete(tx, ex, {}, Fence("running", ex.lease_epoch))
    assert app.get_run(sub.run_id).status == RunStatus.DONE  # type: ignore[union-attr]
