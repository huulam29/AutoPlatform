"""Dry runs: the real engine with stubbed effects. Uses the feature-factory template (a
spec -> build -> 4 parallel reviews -> loop / approve / escalate pipeline)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentline import App, SqliteLedger
from agentline.api.server import create_server
from agentline.builtins import default_registry
from agentline.dryrun import dry_run, from_schema
from agentline.pipeline.spec import load_spec

FACTORY = Path(__file__).parents[2] / "src/agentline/templates/feature-factory/pipeline.yaml"


def run(routes: dict[str, list[str]]) -> dict[str, Any]:
    payload = {"feature": "dark mode", "repo": "acme/app"}
    return asyncio.run(
        dry_run(load_spec(FACTORY), default_registry(), payload=payload, routes=routes)
    )


def nodes(r: dict[str, Any]) -> dict[str, tuple[str, int]]:
    return {n["node"]: (n["status"], n["runs"]) for n in r["nodes"]}


def merges(r: dict[str, Any]) -> int:
    return sum(1 for c in r["calls"] if c["what"].startswith("github.merge_pr"))


def test_one_review_round_then_approve() -> None:
    r = run({"reviews": ["CHANGES", "PASS"], "decide": ["APPROVE"]})
    assert r["status"] == "done"
    n = nodes(r)
    assert n["build"] == ("done", 2) and n["security"] == ("done", 2) and n["gather"] == ("done", 2)
    assert n["approver"] == ("done", 1) and n["needs_human"] == ("skipped", 0)
    assert merges(r) == 1
    assert r["not_loaded"]["skills"]["spec"] == [
        "spec-driven-development",
        "planning-and-task-breakdown",
    ]
    # the four reviewers ran in parallel after the PR: all four agents saw the PR url
    prompts = [
        c["what"] for c in r["calls"] if c["node"] in ("security", "ux", "ui_design", "code")
    ]
    assert len(prompts) == 8 and all("<pr.url>" in p for p in prompts)
    # round two's builder prompt has the review findings block
    builds = [c["what"] for c in r["calls"] if c["node"] == "build"]
    assert "Review round" not in builds[0] and "Review round" in builds[1]


def test_changes_three_times_stops_after_two_rounds() -> None:
    r = run({"reviews": ["CHANGES", "CHANGES", "CHANGES"]})
    assert r["status"] == "needs-review"
    assert nodes(r)["build"] == ("done", 3) and nodes(r)["reviews"][0] == "needs-review"
    assert merges(r) == 0


def test_escalation_goes_through_the_human_gate() -> None:
    r = run({"reviews": ["PASS"], "decide": ["ESCALATE"]})
    assert r["status"] == "done" and nodes(r)["needs_human"] == ("done", 1) and merges(r) == 1
    assert any(
        j["message"] == "waiting -> done" and j["node"] == "needs_human" for j in r["journal"]
    )


def test_from_schema() -> None:
    props = {
        "v": {"enum": ["PASS", "CHANGES"]},
        "n": {"type": "integer"},
        "l": {"type": "array", "items": {"type": "string"}},
    }
    schema = {"type": "object", "properties": props}
    assert from_schema(schema, "x") == {"v": "PASS", "n": 0, "l": ["dry-run x.l[0]"]}


def test_api(tmp_path: Path) -> None:
    import shutil

    pipelines = tmp_path / "pipelines"
    shutil.copytree(FACTORY.parent, pipelines / "factory")
    app = App(SqliteLedger(tmp_path / "l.db"))
    c = TestClient(create_server(app, [], pipelines_dir=pipelines))
    r = c.post("/api/dryrun", json={"name": "factory/pipeline", "routes": {"decide": ["ESCALATE"]}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"
    bad = (
        (pipelines / "factory/pipeline.yaml")
        .read_text()
        .replace("in: [spec, verdict?]", "in: [spec]")
    )
    r = c.post("/api/dryrun", json={"name": "factory/pipeline", "source": bad})
    assert r.status_code == 422 and "verdict" in r.text
    assert c.post("/api/dryrun", json={"name": "ghost"}).status_code == 404
    assert app.get_run  # the real ledger is untouched by dry runs
    with app.ledger.tx() as tx:
        assert tx.list_runs() == []


@pytest.mark.parametrize("template", ["ticket-to-eod", "pr-review", "nightly-tests"])
def test_shipped_templates_dry_run(template: str) -> None:
    path = FACTORY.parents[1] / template / "pipeline.yaml"
    r = asyncio.run(dry_run(load_spec(path), default_registry(), payload={"issue_key": "X-1"}))
    assert r["status"] in ("done", "needs-review"), r
