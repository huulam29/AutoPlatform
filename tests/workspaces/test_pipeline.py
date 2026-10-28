"""A YAML pipeline end to end on the real engine: `local` workspace + `shell` runner make a
commit, the branch artifact reaches the next node; missing tools park with a proposal."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentline import App, SqliteLedger
from agentline.builtins import default_registry
from agentline.config import Config
from agentline.pipeline import build_pipeline, parse_spec
from agentline.registry import RunRequest
from agentline.runners import RunnerError

from .helpers import git, make_remote

PIPELINE = """\
version: 1
name: local-impl
budget_usd: 1.0
nodes:
  implement:
    type: agent
    runner: shell
    prompt: "Implement {{payload.ticket}} (iteration {{node.iteration}})"
    out: branch
    allowed_tools: [read, edit, bash]
    workspace: { type: local, repo: app }
    with:
      cmd: >-
        cat > prompt.txt && git add -A && git commit -q -m "$AGENTLINE_NODE_ID" &&
        echo '{"summary": "implemented"}'
  check:
    type: agent
    runner: python
    prompt: "check {{branch.sha}}"
    in: [branch]
    out: report
    with: { function: "tests.workspaces.test_pipeline:check" }
edges:
  - implement -> check
"""


def check(request: RunRequest) -> dict[str, Any]:
    b = request.inputs["branch"]
    return {"checked": b["sha"], "prompt": request.prompt, "mirror": b["mirror"]}


async def costly_failure(request: RunRequest) -> None:
    raise RunnerError("model said no", cost_usd=0.25, transcript="the transcript")


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    return make_remote(tmp_path / "remote")


@pytest.fixture
def make_app(tmp_path: Path, remote: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AGENTLINE_HOME", str(tmp_path / "home"))
    config = Config.from_dict({"repos": {"app": {"remote": str(remote)}}})
    ledgers: list[SqliteLedger] = []

    def make(source: str) -> App:
        ledger = SqliteLedger(tmp_path / f"ledger{len(ledgers)}.db")
        ledgers.append(ledger)
        app = App(ledger, backoff_base=0.0)
        spec = parse_spec(source, base_dir=tmp_path)
        app.register_pipeline(build_pipeline(spec, default_registry(config), config))
        return app

    yield make
    for ledger in ledgers:
        ledger.close()


def drain(app: App) -> None:
    asyncio.run(app.worker(concurrency=2).run_until_idle())


def test_local_workspace_shell_runner_end_to_end(make_app: Any, tmp_path: Path) -> None:
    app = make_app(PIPELINE)
    sub = app.submit_sync("local-impl", {"ticket": "PROJ-7"})
    drain(app)
    run = app.get_run(sub.run_id)
    assert run is not None and run.status == "done", app.executions(sub.run_id)
    branch = app.result(sub.run_id, "implement")
    mirror = tmp_path / "home" / "mirrors" / "app.git"
    assert branch["name"] == f"agentline/{sub.run_id}"
    assert branch["sha"] == git(mirror, "rev-parse", branch["name"])
    assert branch["base_sha"] == git(tmp_path / "remote", "rev-parse", "main")
    assert branch["repo"] == "app" and branch["mirror"] == str(mirror)
    assert branch["summary"] == "implemented"  # dict output merged flat
    assert git(mirror, "show", f"{branch['sha']}:prompt.txt") == "Implement PROJ-7 (iteration 1)"
    assert git(mirror, "log", "-1", "--format=%s", branch["sha"]) == "implement"
    report = app.result(sub.run_id, "check")
    assert report["checked"] == branch["sha"] and report["mirror"] == str(mirror)
    (impl,) = [x for x in app.executions(sub.run_id) if x.node_id == "implement"]
    assert "diff" in impl.detail["attachments"]
    assert impl.detail["workspace"]["branch"] == branch["name"]


def test_missing_tool_parks_with_image_proposal(make_app: Any) -> None:
    source = PIPELINE.replace(
        """cmd: >-
        cat > prompt.txt""",
        """cmd: >-
        psql -c 'select 1' && cat > prompt.txt""",
    )
    app = make_app(source)
    sub = app.submit_sync("local-impl", {"ticket": "PROJ-8"})
    drain(app)
    (ex,) = [x for x in app.executions(sub.run_id) if x.node_id == "implement"]
    if ex.status == "done":  # psql is installed on this machine; nothing to test
        pytest.skip("psql is installed")
    assert ex.status == "needs-review" and ex.reason == "missing_tools"
    assert ex.detail["missing_tools"] == ["psql"]
    assert ex.detail["image_proposal"]["add_apt"] == ["postgresql-client"]


def test_runner_error_cost_is_recorded(make_app: Any) -> None:
    source = """\
version: 1
name: costly
budget_usd: 1.0
nodes:
  think:
    type: agent
    runner: python
    prompt: "go"
    max_attempts: 1
    with: { function: "tests.workspaces.test_pipeline:costly_failure" }
"""
    app = make_app(source)
    sub = app.submit_sync("costly", {})
    drain(app)
    run = app.get_run(sub.run_id)
    assert run is not None and run.cost_usd == pytest.approx(0.25)
    (ex,) = app.executions(sub.run_id)
    assert "model said no" in (ex.error or "")
    # the transcript blob is stored, but worker.fail() doesn't persist notes (Phase 5 / limit)
