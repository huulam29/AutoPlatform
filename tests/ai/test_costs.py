"""Spend reports and budget alerts."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from agentline import App, SqliteLedger, costs
from agentline.api.server import create_server
from agentline.pipeline import build_pipeline, parse_spec
from agentline.registry import Registry, RunRequest, RunResult
from agentline.runners.function import FunctionRunner

SPEC = """\
version: 1
name: spendy
nodes:
  a: { type: agent, runner: fn, prompt: hi, model: cheap }
  b: { type: agent, runner: fn, prompt: hi }
edges:
  - a -> b
"""


def make(tmp_path: Path) -> App:
    reg = Registry()

    async def agent(req: RunRequest) -> RunResult:
        return RunResult(output={}, cost_usd=0.75 if req.node_id == "a" else 0.25)

    reg.add_runner(FunctionRunner("fn", agent))
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0.0)
    app.register_pipeline(build_pipeline(parse_spec(SPEC, base_dir=tmp_path), reg))
    for _ in range(3):
        app.submit_sync("spendy", {})
    asyncio.run(app.worker(concurrency=2).run_until_idle())
    return app


def test_report_by_day_pipeline_node(tmp_path: Path) -> None:
    r = costs.report(make(tmp_path), days=7)
    assert r["total_usd"] == 3.0 and r["month_usd"] == 3.0
    assert r["by_pipeline"] == [{"pipeline": "spendy", "usd": 3.0, "runs": 3}]
    assert [n["node"] for n in r["top_nodes"]] == ["spendy / a", "spendy / b"]
    assert sum(d["usd"] for d in r["by_day"]) == 3.0 and len(r["by_day"]) == 7
    assert sum(m["usd"] for m in r["by_model"]) == 3.0


def test_budget_alerts_once_per_threshold(tmp_path: Path) -> None:
    app = make(tmp_path)  # $3 spent
    assert costs.due_alerts(app) == []  # no budget
    costs.set_budget(5)
    assert [a["threshold"] for a in costs.due_alerts(app)] == [50]  # 60%
    assert costs.due_alerts(app) == []
    costs.set_budget(3)
    assert [a["threshold"] for a in costs.due_alerts(app)] == [80, 100]
    (tmp_path / "p").mkdir()
    c = TestClient(create_server(app, [], pipelines_dir=tmp_path / "p"))
    assert c.get("/api/costs?days=3").json()["budget_usd"] == 3
    assert c.put("/api/budget", json={"monthly_usd": None}).json() == {"budget_usd": None}
