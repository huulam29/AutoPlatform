"""Shared CLI test harness: temp home/db/config/pipelines and `cli.run(*argv)`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agentline.app import App
from agentline.cli import main
from agentline.ledger import SqliteLedger

CONFIG = """\
# agentline config for tests
[repos.app]
remote = "/nonexistent/app.git"

[environments.eod]
deploy = "echo deploying {sha} of {branch}"
test = "echo testing"

[images.python]
from = "agentline/runner-base"
apt = ["python3", "python3-venv"]

# node toolchain, keep as is
[images.node]
apt = ["nodejs"]
"""

GATED = """\
version: 1
name: gated
budget_usd: 1.0
nodes:
  plan:
    type: agent
    runner: fake
    prompt: "plan {{payload.ticket}}"
    out: plan
  approve:
    type: gate
    in: [plan]
    prompt: "Deploy {{payload.branch}} ({{plan.summary}})?"
  deploy:
    type: action
    uses: env.deploy
    with: { env: eod, branch: "{{payload.branch}}", sha: "{{payload.sha}}" }
    out: deployment
edges:
  - plan -> approve -> deploy
"""

REVIEW = """\
version: 1
name: review
nodes:
  check:
    type: agent
    runner: fake
    prompt: "mode:review"
    out: report
  after:
    type: agent
    runner: fake
    prompt: "after {{report.summary}}"
    in: [report]
    out: final
edges:
  - check -> after
"""

MISSING = """\
version: 1
name: missing
nodes:
  implement:
    type: agent
    runner: fake
    prompt: "mode:missing"
    out: branch
    allowed_tools: [read, edit, bash]
    workspace: { type: fakebox, repo: app }
edges: []
"""

FLAKY = """\
version: 1
name: flaky
nodes:
  make:
    type: action
    uses: fake.flaky
    out: thing
edges: []
"""


@dataclass
class Cli:
    root: Path
    capsys: pytest.CaptureFixture[str]

    @property
    def db(self) -> Path:
        return self.root / "home" / "agentline.db"

    @property
    def config(self) -> Path:
        return self.root / "agentline.toml"

    @property
    def pipelines(self) -> Path:
        return self.root / "pipelines"

    def write_pipeline(self, name: str, text: str) -> Path:
        p = self.pipelines / f"{name}.yaml"
        p.write_text(text)
        return p

    def run(self, *argv: str) -> tuple[int, str, str]:
        self.capsys.readouterr()
        code = main(["--config", str(self.config), "--pipelines", str(self.pipelines), *argv])
        out, err = self.capsys.readouterr()
        return code, out, err

    def ok(self, *argv: str) -> str:
        code, out, err = self.run(*argv)
        assert code == 0, f"exit {code}\nstdout:\n{out}\nstderr:\n{err}"
        return out

    def json(self, *argv: str) -> Any:
        return json.loads(self.ok(*argv, "--json"))

    def submit(self, pipeline: str, payload: dict[str, Any] | None = None, *extra: str) -> str:
        out = self.ok("run", pipeline, "--payload", json.dumps(payload or {}), *extra)
        return out.split()[1]

    def app(self) -> App:
        return App(SqliteLedger(self.db))


def make_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> Cli:
    monkeypatch.setenv("AGENTLINE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AGENTLINE_DB", raising=False)
    monkeypatch.delenv("AGENTLINE_CONFIG", raising=False)
    monkeypatch.setenv("AGENTLINE_REGISTRY", "tests.cli.fakes:registry")
    monkeypatch.setenv("AGENTLINE_USER", "alice")
    monkeypatch.setenv("AGENTLINE_DOCKER", str(tmp_path / "no-docker"))
    cli = Cli(tmp_path, capsys)
    cli.config.write_text(CONFIG)
    cli.pipelines.mkdir()
    for name, text in (("gated", GATED), ("review", REVIEW), ("missing", MISSING)):
        cli.write_pipeline(name, text)
    from tests.cli import fakes

    fakes.FLAKY["calls"] = 0
    return cli
