"""Lineage after running examples/ticket-to-eod with one review loop."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentline import App, RunStatus, SqliteLedger
from agentline.pipeline import load_pipeline

from ..pipeline.fakes import World, make_registry
from ..pipeline.test_spec import EXAMPLE


@pytest.fixture
def app(tmp_path: Path) -> Iterator[App]:
    ledger = SqliteLedger(tmp_path / "ledger.db")
    app = App(ledger, backoff_base=0.0, artifact_root=tmp_path / "artifacts")
    app.register_pipeline(load_pipeline(EXAMPLE, make_registry(World())))
    yield app
    ledger.close()


def drain(app: App) -> None:
    asyncio.run(app.worker(concurrency=3).run_until_idle())


def run_example(app: App) -> str:
    sub = app.submit_sync("ticket-to-eod", {"issue_key": "PROJ-1"})
    drain(app)
    for state in ("changes_requested", "approved"):
        (key,) = [x.correlation_key for x in app.executions(sub.run_id) if x.status == "waiting"]
        asyncio.run(app.deliver_event(key or "", {"state": state, "comments": ["c"]}))
        drain(app)
    asyncio.run(app.approve(sub.run_id, actor="huu"))
    drain(app)
    run = app.get_run(sub.run_id)
    assert run is not None and run.status == RunStatus.DONE
    return sub.run_id


def by_node(lineage: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {e["node"]: e for e in lineage}


def test_lineage_of_the_review_loop(app: App) -> None:
    run_id = run_example(app)
    lin = by_node(app.lineage(run_id))

    impl2 = lin["implement#2"]
    reads = {r["name"]: r for r in impl2["reads"]}
    assert set(reads) == {"ticket", "plan", "review"}  # only declared inputs
    assert reads["review"]["from"] == "await_review#1"
    assert reads["plan"]["from"] == "plan#1"
    assert reads["plan"]["summary"] == (
        '{summary: "Fix login", approach: "patch it", steps: [2 items]}'
    )
    assert {w["name"] for w in impl2["writes"]} == {"branch", "transcript.first"}

    assert {r["name"] for r in lin["implement#1"]["reads"]} == {"ticket", "plan"}

    assert lin["deploy#1"]["status"] == "skipped"  # dead path after route#1 looped back
    (deploy_read,) = lin["deploy#2"]["reads"]
    assert deploy_read["name"] == "branch" and deploy_read["from"] == "implement#2"
    branch2 = next(w for w in impl2["writes"] if w["name"] == "branch")
    assert deploy_read["hash"] == branch2["hash"]
    assert app.artifact(deploy_read["hash"])["value"]["sha"] == "sha2"  # type: ignore[index]

    assert [w["name"] for w in lin["approve#2"]["writes"]] == ["approve"]
    assert lin["approve#1"]["status"] == "skipped" and lin["approve#1"]["writes"] == []


def test_result_ref_is_the_artifact_hash(app: App) -> None:
    run_id = run_example(app)
    plan = next(x for x in app.executions(run_id) if x.node_id == "plan")
    art = app.artifact(plan.result_ref or "")
    assert art is not None and art["kind"] == "json" and art["storage"] == "inline"
    assert art["value"] == app.result(run_id, "plan")
    assert art["media_type"] == "application/json"


def test_attachments_are_file_artifacts(app: App) -> None:
    run_id = run_example(app)
    plan = next(x for x in app.executions(run_id) if x.node_id == "plan")
    att = plan.detail["attachments"]["transcript.first"]
    assert att["size"] == len(b"planned") and att["summary"] == "planned\n(1 line)"
    art = app.artifact(att["ref"])
    assert art is not None
    assert (art["kind"], art["media_type"], art["value"]) == ("file", "text/plain", b"planned")


def test_artifacts_of_a_run(app: App) -> None:
    run_id = run_example(app)
    arts = app.artifacts(run_id)
    names = [(a["node"], a["name"]) for a in arts]
    assert ("implement#1", "branch") in names and ("implement#2", "branch") in names
    assert ("test#2", "test_report") in names
    assert all("summary" in a and "size" in a for a in arts)
    assert len(names) == len(set(names))
