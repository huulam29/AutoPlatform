"""Run YAML pipelines end to end on the engine with fake agents and actions."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentline import App, RunStatus, SqliteLedger
from agentline.pipeline import build_pipeline, load_pipeline, parse_spec

from .fakes import World, make_registry
from .test_spec import EXAMPLE


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def app(tmp_path: Path, world: World) -> Iterator[App]:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    app = App(ledger, backoff_base=0.0)
    app.register_pipeline(load_pipeline(EXAMPLE, make_registry(world)))
    yield app
    ledger.close()


def drain(app: App) -> None:
    asyncio.run(app.worker(concurrency=3).run_until_idle())


def status(app: App, run_id: str) -> str:
    run = app.get_run(run_id)
    assert run is not None
    return run.status


def waiting_keys(app: App, run_id: str) -> list[str]:
    return [x.correlation_key or "" for x in app.executions(run_id) if x.status == "waiting"]


def review(app: App, run_id: str, state: str, comments: list[str] | None = None) -> None:
    (key,) = waiting_keys(app, run_id)
    assert key.startswith("github:pr:")
    asyncio.run(app.deliver_event(key, {"state": state, "comments": comments or []}))


def test_ticket_to_eod_with_one_review_loop(app: App, world: World) -> None:
    sub = app.submit_sync("ticket-to-eod", {"issue_key": "PROJ-1"}, dedupe_key="PROJ-1")
    drain(app)
    assert status(app, sub.run_id) == RunStatus.WAITING
    review(app, sub.run_id, "changes_requested", ["add a test for the empty password"])
    drain(app)
    assert world.agent_calls == [("plan", 1), ("implement", 1), ("implement", 2)]
    assert "add a test for the empty password" in world.prompts["implement#2"]
    assert "Review feedback" not in world.prompts["implement#1"]
    review(app, sub.run_id, "approved")
    drain(app)
    gates = app.waiting_gates(sub.run_id)
    assert [g.node_id for g in gates] == ["approve"]
    assert gates[0].detail["prompt"] == "Deploy agentline/PROJ-1 (PR #1) to EOD?"
    assert world.deploys == []
    asyncio.run(app.approve(sub.run_id, actor="huu"))
    drain(app)
    assert status(app, sub.run_id) == RunStatus.DONE
    assert world.pr_creates == 1 and list(world.prs) == ["agentline/PROJ-1"]
    assert world.deploys == [{"env": "eod", "sha": "sha2"}]  # the second iteration's code
    assert app.result(sub.run_id, "test") == {"passed": True, "env": "eod"}
    assert app.result(sub.run_id, "approve")["by"] == "huu"
    keys = {x.key: x.status for x in app.executions(sub.run_id)}
    assert keys["approve#1"] == "skipped" and keys["approve#2"] == "done"
    assert keys["implement#2"] == "done" and keys["open_pr#2"] == "done"
    run = app.get_run(sub.run_id)
    assert run is not None and run.cost_usd == pytest.approx(0.01 + 0.05 * 2)


def test_transcripts_and_runner_notes_are_recorded(app: App) -> None:
    sub = app.submit_sync("ticket-to-eod", {"issue_key": "PROJ-2"})
    drain(app)
    plan = next(x for x in app.executions(sub.run_id) if x.node_id == "plan")
    att = plan.detail["attachments"]["transcript.first"]
    with app.ledger.tx() as tx:
        assert tx.get_blob(att["ref"]) == b"planned"
    assert plan.detail["runner"] == "claude-code"


def test_review_loop_bound(app: App) -> None:
    sub = app.submit_sync("ticket-to-eod", {"issue_key": "PROJ-3"})
    drain(app)
    for _ in range(4):
        review(app, sub.run_id, "changes_requested")
        drain(app)
    assert status(app, sub.run_id) == RunStatus.NEEDS_REVIEW
    (stuck,) = [x for x in app.executions(sub.run_id) if x.status == "needs-review"]
    assert stuck.key == "route#4" and stuck.reason == "loop_exhausted"


def test_gate_rejection_and_approver_list(tmp_path: Path, world: World) -> None:
    src = """\
name: gated
nodes:
  ok: { type: gate, prompt: "Ship?", approvers: [alice] }
  deploy: { type: action, uses: env.deploy, with: { env: eod } }
edges:
  - ok -> deploy
"""
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0)
    app.register_pipeline(build_pipeline(parse_spec(src), make_registry(world)))
    sub = app.submit_sync("gated", {})
    drain(app)
    with pytest.raises(PermissionError):
        asyncio.run(app.approve(sub.run_id, actor="mallory"))
    asyncio.run(app.reject(sub.run_id, actor="alice", comment="not today"))
    run = app.get_run(sub.run_id)
    assert run is not None and run.status == RunStatus.DEAD
    assert "rejected by alice: not today" in (run.error or "")
    assert world.deploys == []


def test_schema_repair_attempt(app: App, world: World) -> None:
    world.plan_outputs = [{"summary": "x"}, {"summary": "Fix it", "approach": "a", "steps": ["s"]}]
    sub = app.submit_sync("ticket-to-eod", {"issue_key": "PROJ-4"})
    drain(app)
    assert world.agent_calls[:3] == [("plan", 1), ("plan", 1), ("implement", 1)]
    plan = next(x for x in app.executions(sub.run_id) if x.node_id == "plan")
    assert plan.detail["schema_errors.first"][0] == "$: missing required property 'approach'"
    assert "did not match the required output schema" in world.prompts["plan#1"]


def test_schema_failure_after_repair_is_a_failed_attempt(app: App, world: World) -> None:
    world.plan_outputs = [{"summary": "x"}] * 6
    sub = app.submit_sync("ticket-to-eod", {"issue_key": "PROJ-5"})
    drain(app)
    assert status(app, sub.run_id) == RunStatus.DEAD
    plan = next(x for x in app.executions(sub.run_id) if x.node_id == "plan")
    assert plan.attempt == 3 and "after a repair attempt" in (plan.error or "")


def test_missing_tools_parks_for_review(app: App, world: World) -> None:
    world.missing_tools = ["psql"]
    sub = app.submit_sync("ticket-to-eod", {"issue_key": "PROJ-6"})
    drain(app)
    (ex,) = [x for x in app.executions(sub.run_id) if x.status == "needs-review"]
    assert ex.node_id == "plan" and ex.reason == "missing_tools"
    assert ex.detail["missing_tools"] == ["psql"]
    assert "transcript.first" in ex.detail["attachments"]


def test_parallel_branches_and_default_route(tmp_path: Path, world: World) -> None:
    src = """\
name: fanout
nodes:
  t: { type: action, uses: jira.get_ticket, with: { key: K-1 }, out: ticket }
  lint: { type: action, uses: env.test, with: { env: lint }, out: lint }
  unit: { type: action, uses: env.test, with: { env: unit }, out: unit }
  r: { type: router, switch: ticket.key }
  special: { type: action, uses: env.test, with: { env: special } }
  normal: { type: action, uses: env.test, with: { env: normal } }
edges:
  - t -> lint -> r
  - t -> unit -> r
  - { from: r, to: special, when: K-9 }
  - { from: r, to: normal, when: default }
"""
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0)
    app.register_pipeline(build_pipeline(parse_spec(src), make_registry(world)))
    sub = app.submit_sync("fanout", {})
    drain(app)
    keys = {x.key: x.status for x in app.executions(sub.run_id)}
    assert keys == {
        "t#1": "done",
        "lint#1": "done",
        "unit#1": "done",
        "r#1": "done",
        "special#1": "skipped",
        "normal#1": "done",
    }
    assert app.result(sub.run_id, "r") == {"route": "K-1", "switch": "ticket.key", "value": "K-1"}


def test_template_error_at_runtime_is_permanent(tmp_path: Path, world: World) -> None:
    src = """\
name: tmpl
nodes:
  a: { type: action, uses: jira.get_ticket, with: { key: "{{payload.missing}}" } }
"""
    app = App(SqliteLedger(tmp_path / "l.db"), backoff_base=0)
    app.register_pipeline(build_pipeline(parse_spec(src), make_registry(world)))
    sub = app.submit_sync("tmpl", {})
    drain(app)
    (ex,) = app.executions(sub.run_id)
    assert ex.status == "dead" and "payload.missing" in (ex.error or "")
    assert ex.attempt == 1
