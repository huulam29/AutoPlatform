"""for_each: one task per list item, max_parallel at a time, retries rerun only failed tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentline import App, SqliteLedger
from agentline.pipeline import build_pipeline, parse_spec
from agentline.pipeline.spec import SpecError
from agentline.pipeline.validate import validate_spec
from agentline.registry import ActionCall, Registry, RunRequest, RunResult
from agentline.runners.function import FunctionRunner

SPEC = """\
name: fan
nodes:
  plan: { type: action, uses: make.plan, out: plan }
  work:
    type: agent
    runner: fn
    prompt: "Do {{item.title}} ({{item_index}})"
    in: [plan]
    out: done
    for_each: plan.steps
    max_parallel: 2
    max_attempts: 2
  ship: { type: action, uses: notify, in: [done], out: shipped }
edges:
  - plan -> work -> ship
"""


def make(tmp_path: Path, flaky: set[int]) -> tuple[App, dict[str, Any]]:
    seen: dict[str, Any] = {"prompts": [], "peak": 0, "live": 0, "calls": {}}
    reg = Registry()

    @reg.action("make.plan")
    async def plan(ctx: Any, call: ActionCall) -> dict[str, Any]:
        return {"steps": [{"title": t} for t in ("a", "b", "c", "d", "e")]}

    @reg.action("notify")
    async def notify(ctx: Any, call: ActionCall) -> dict[str, Any]:
        return {"count": len(call.inputs["done"])}

    async def agent(req: RunRequest) -> RunResult:
        seen["live"] += 1
        seen["peak"] = max(seen["peak"], seen["live"])
        await asyncio.sleep(0.02)
        seen["live"] -= 1
        seen["prompts"].append(req.prompt)
        idx = int(req.prompt.split("(")[1].rstrip(")"))
        seen["calls"][idx] = seen["calls"].get(idx, 0) + 1
        if idx in flaky:
            flaky.discard(idx)  # fails once
            raise RuntimeError(f"step {idx} broke")
        return RunResult(output={"step": idx})

    reg.add_runner(FunctionRunner("fn", agent))
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0.0)
    app.register_pipeline(build_pipeline(parse_spec(SPEC, base_dir=tmp_path), reg))
    return app, seen


def drain(app: App) -> None:
    asyncio.run(app.worker(concurrency=2).run_until_idle())


def test_tasks_run_in_parallel_and_collect_in_order(tmp_path: Path) -> None:
    app, seen = make(tmp_path, set())
    sub = app.submit_sync("fan", {})
    drain(app)
    assert app.get_run(sub.run_id).status == "done"
    assert app.result(sub.run_id, "work") == [{"step": i} for i in range(5)]
    assert app.result(sub.run_id, "ship") == {"count": 5}
    assert seen["peak"] == 2  # max_parallel
    assert "Do c (2)" in seen["prompts"]
    with app.ledger.tx() as tx:
        tasks = tx.list_tasks(sub.run_id)
        journal = tx.list_journal(sub.run_id, node_id="work")
    assert [(t.idx, t.status, t.attempts) for t in tasks] == [(i, "done", 1) for i in range(5)]
    msgs = [j.message for j in journal]
    assert "task 3 started (attempt 1)" in msgs and "task 3 done" in msgs
    assert any(m.endswith("-> done") for m in msgs)


def test_retry_reruns_only_the_failed_task(tmp_path: Path) -> None:
    app, seen = make(tmp_path, {1, 3})
    sub = app.submit_sync("fan", {})
    drain(app)
    assert app.get_run(sub.run_id).status == "done"  # second attempt
    assert seen["calls"] == {0: 1, 1: 2, 2: 1, 3: 2, 4: 1}
    with app.ledger.tx() as tx:
        t = {x.idx: x for x in tx.list_tasks(sub.run_id)}
        journal = [j for j in tx.list_journal(sub.run_id, node_id="work") if j.task == 1]
    assert t[1].attempts == 2 and t[1].status == "done" and t[0].attempts == 1
    assert [j.message for j in journal] == [
        "task 1 started (attempt 1)", "task 1 failed", "task 1 started (attempt 2)", "task 1 done"
    ]  # fmt: skip
    assert journal[1].detail["error"] == "RuntimeError: step 1 broke"


def test_validation() -> None:
    bad = SPEC.replace("in: [plan]\n    out: done", "out: done")
    msgs = [i.message for i in validate_spec(parse_spec(bad))]
    assert any("for_each reads 'plan'" in m for m in msgs)
    bad = SPEC.replace("max_parallel: 2", "max_parallel: 0")
    with pytest.raises(SpecError, match="max_parallel: must be an integer >= 1"):
        parse_spec(bad)


def test_dead_node_retry_keeps_done_tasks_and_api_counts(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from agentline.api.server import create_server

    app, seen = make(tmp_path, {2})
    app.graph("fan").nodes["work"].max_attempts = 1  # die on the first failure
    sub = app.submit_sync("fan", {})
    drain(app)
    assert app.get_run(sub.run_id).status == "dead"
    (tmp_path / "p").mkdir()
    c = TestClient(create_server(app, [], pipelines_dir=tmp_path / "p"))
    d = c.get(f"/api/runs/{sub.run_id}").json()
    assert d["node_counts"]["work"] == {
        "total": 5,
        "pending": 0,
        "running": 0,
        "done": 4,
        "failed": 1,
    }
    assert d["node_counts"]["plan"]["done"] == 1
    assert {t["idx"]: t["status"] for t in d["tasks"]}[2] == "failed"
    j = c.get(f"/api/runs/{sub.run_id}/journal?node=work").json()["journal"]
    assert any(e["message"] == "task 2 failed" for e in j)
    r = c.post(f"/api/runs/{sub.run_id}/retry", json={"node": "work"}).json()
    assert r["retried"] == 1
    drain(app)
    assert app.get_run(sub.run_id).status == "done"
    assert seen["calls"] == {0: 1, 1: 1, 2: 2, 3: 1, 4: 1}  # only the failed task ran again
    j = c.get(f"/api/runs/{sub.run_id}/journal").json()["journal"]
    assert any(e["kind"] == "retry" and "via app" in e["message"] for e in j)
    assert c.post("/api/runs/nope/retry", json={}).status_code == 404
