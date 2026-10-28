"""Issues: one tracked issue per problem, reopened when a fixed problem comes back."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.server import create_server
from agentline.errors import ValidationError
from agentline.incidents import IncidentStore
from agentline.issues import IssueStore
from agentline.ledger.base import Run


def test_ensure_creates_once_and_reopens_fixed_on_recurrence(tmp_path: Path) -> None:
    s = IssueStore(tmp_path / "issues")
    a, what = s.ensure("incident", "abc", title="KeyError: x", seen_at=10)
    assert what == "created" and a["number"] == 1 and a["status"] == "open"
    assert s.ensure("incident", "abc", title="KeyError: x", seen_at=10)[1] == "unchanged"
    b, _ = s.ensure("run", "r1", title="Run r1", seen_at=5)
    assert b["number"] == 2
    s.update(1, status="in_progress")
    s.update(1, note="looks like a missing default")
    s.update(1, status="fixed", note="fixed in 1.2")
    rec, what = s.ensure("incident", "abc", title="KeyError: x", seen_at=20)
    assert what == "reopened" and rec["status"] == "open" and rec["occurrences"] == 2
    assert [h["to"] for h in rec["history"]] == ["open", "in_progress", "fixed", "open"]
    assert rec["history"][2]["note"] == "fixed in 1.2"
    assert rec["notes"][0]["text"] == "looks like a missing default"
    # won't fix stays closed when it happens again
    s.update(2, status="wont_fix")
    assert s.ensure("run", "r1", title="Run r1", seen_at=9)[0]["status"] == "wont_fix"
    assert [i["number"] for i in s.list("active", app=True)] == [1]
    assert [i["number"] for i in s.list("closed")] == [2]
    assert s.list("active") == []  # #1 is an incident's: an app problem, not on Problems
    with pytest.raises(ValidationError):
        s.update(1, status="done")
    with pytest.raises(KeyError):
        s.update(99, note="x")
    assert s.delete(2) and not s.delete(2)


def test_api_syncs_failed_runs_not_incidents(tmp_path: Path) -> None:
    incidents = IncidentStore(tmp_path / "home" / "incidents")
    app = App(SqliteLedger(tmp_path / "l.db"), incidents=incidents)
    (tmp_path / "pipelines").mkdir()
    incidents.record("worker_error", title="TypeError in step", message="bad")
    with app.ledger.tx() as tx:
        tx.insert_run(
            Run(
                id="r1",
                flow="ticket",
                flow_hash="h",
                payload={},
                status="dead",
                error="boom",
                created_at=1,
                updated_at=2,
            )
        )
        tx.insert_run(
            Run(
                id="r2",
                flow="ticket",
                flow_hash="h",
                payload={},
                status="done",
                created_at=1,
                updated_at=2,
            )
        )
    c = TestClient(create_server(app, [], pipelines_dir=tmp_path / "pipelines"))
    body = c.get("/api/issues").json()
    # the crash report is an app problem: no issue for it (it's on App reports)
    assert body["synced"] == {"created": 1, "reopened": 0}
    assert {i["source"]["kind"] for i in body["issues"]} == {"run"}
    assert body["counts"]["open"] == 1
    reports = c.get("/api/app-reports").json()["reports"]
    assert [r["summary"] for r in reports] == ["TypeError in step"]
    run_issue = next(i for i in body["issues"] if i["source"]["kind"] == "run")
    assert run_issue["source"]["ref"] == "r1" and run_issue["detail"] == "boom"
    n = run_issue["number"]
    r = c.patch(f"/api/issues/{n}", json={"status": "fixed", "note": "retried"})
    assert r.status_code == 200 and r.json()["status"] == "fixed"
    assert c.get("/api/issues?status=closed").json()["issues"][0]["number"] == n
    assert c.get("/api/issues").json()["synced"] == {"created": 0, "reopened": 0}
    # the run fails again later: the issue reopens
    with app.ledger.tx() as tx:
        tx.update_run("r1", updated_at=50)
    again = c.get("/api/issues").json()
    assert again["synced"]["reopened"] == 1 and again["counts"]["fixed"] == 0
    assert c.patch(f"/api/issues/{n}", json={"status": "nope"}).status_code == 422
    assert c.get("/api/issues/999").status_code == 404
    assert c.delete(f"/api/issues/{n}").json() == {"deleted": n}
    assert (tmp_path / "home" / "issues").is_dir()


def test_fix_request_for_a_failed_run(tmp_path: Path) -> None:
    from agentline.pipeline import build_pipeline, parse_spec
    from agentline.registry import ActionCall, Registry

    incidents = IncidentStore(tmp_path / "home" / "incidents")
    app = App(SqliteLedger(tmp_path / "l.db"), incidents=incidents, backoff_base=0.0)
    reg = Registry()

    @reg.action("boom")
    async def boom(ctx: object, call: ActionCall) -> dict[str, str]:
        raise RuntimeError("the API key was refused")

    spec = "version: 1\nname: p\nnodes:\n  a: { type: action, uses: boom, max_attempts: 1 }\n"
    app.register_pipeline(build_pipeline(parse_spec(spec, base_dir=tmp_path), reg))
    sub = app.submit_sync("p", {})
    import asyncio

    asyncio.run(app.worker(concurrency=1).run_until_idle())
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "p.yaml").write_text(spec)
    c = TestClient(create_server(app, [], pipelines_dir=tmp_path / "pipelines"))
    issue = next(i for i in c.get("/api/issues").json()["issues"] if i["source"]["kind"] == "run")
    fix = c.get(f"/api/issues/{issue['number']}/fix").json()
    assert fix["pipeline"] == "p" and fix["run"] == sub.run_id
    assert "the API key was refused" in fix["prompt"] and "retry action" in fix["prompt"]
    assert "Node a" in fix["prompt"] and "running -> dead" in fix["prompt"]
    assert c.get(f"/api/runs/{sub.run_id}/fix").json()["pipeline"] == "p"
    assert c.get("/api/issues/999/fix").status_code == 404
