"""Budgets and tool permissions end to end through YAML and the CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.cli.helpers import Cli

FAKE_CLAUDE = Path(__file__).resolve().parents[1] / "runners" / "fake_claude.py"


def _show(cli: Cli, run_id: str) -> dict[str, Any]:
    return cli.json("runs", "show", run_id)  # type: ignore[no-any-return]


def _run(cli: Cli, name: str, text: str) -> dict[str, Any]:
    cli.write_pipeline(name, text)
    code, out, err = cli.run("run", name, "--wait", "--timeout", "20")
    assert code == 0, out + err
    return _show(cli, out.split()[1])


# -- budgets -----------------------------------------------------------------------------------


def test_run_budget_exceeded_goes_to_needs_review(cli: Cli) -> None:
    v = _run(
        cli,
        "pricey",
        "version: 1\nname: pricey\nbudget_usd: 0.05\nnodes:\n"
        "  a: { type: agent, runner: fake, prompt: go, out: x, with: { cost: 0.1 } }\n"
        "  b: { type: agent, runner: fake, prompt: next, in: [x], out: y }\n"
        "edges:\n  - a -> b\n",
    )
    assert v["run"]["status"] == "needs-review" and v["run"]["cost_usd"] == pytest.approx(0.1)
    a = v["executions"][0]
    assert a["status"] == "needs-review" and a["reason"] == "budget_exceeded"
    assert a["detail"]["scope"] == "run" and a["detail"]["budget_usd"] == 0.05
    assert a["attempt"] == 1  # no retries burned on a budget stop
    assert len(v["executions"]) == 1  # b never ran
    show = cli.ok("runs", "show", v["run"]["id"])
    assert "budget:   run spent $0.10 of $0.05" in show


def test_node_budget_exceeded(cli: Cli) -> None:
    v = _run(
        cli,
        "nodebudget",
        "version: 1\nname: nodebudget\nbudget_usd: 1.0\nnodes:\n"
        "  a: { type: agent, runner: fake, prompt: go, out: x, budget_usd: 0.02,"
        " with: { cost: 0.03 } }\nedges: []\n",
    )
    a = v["executions"][0]
    assert a["reason"] == "budget_exceeded" and a["detail"]["scope"] == "node"
    assert a["detail"]["budget_usd"] == 0.02


def test_under_budget_completes(cli: Cli) -> None:
    v = _run(
        cli,
        "cheap",
        "version: 1\nname: cheap\nbudget_usd: 0.05\nnodes:\n"
        "  a: { type: agent, runner: fake, prompt: go, out: x, with: { cost: 0.02 } }\n"
        "  b: { type: agent, runner: fake, prompt: go, in: [x], out: y, with: { cost: 0.02 } }\n"
        "edges:\n  - a -> b\n",
    )
    assert v["run"]["status"] == "done" and v["run"]["cost_usd"] == pytest.approx(0.04)


@pytest.mark.parametrize("where", ["run", "node"])
def test_anthropic_runner_refuses_at_zero_remaining(
    cli: Cli, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")  # would fail if called
    if where == "run":  # a spends the whole run budget, leaving exactly 0 for b
        text = (
            "version: 1\nname: zero\nbudget_usd: 0.05\nnodes:\n"
            "  a: { type: agent, runner: fake, prompt: go, out: x, with: { cost: 0.05 } }\n"
            "  b: { type: agent, runner: anthropic, prompt: 'summarize {{x.summary}}',"
            " in: [x], out: y }\nedges:\n  - a -> b\n"
        )
    else:
        text = (
            "version: 1\nname: zero\nnodes:\n"
            "  a: { type: agent, runner: fake, prompt: go, out: x }\n"
            "  b: { type: agent, runner: anthropic, prompt: hi, in: [x], out: y,"
            " budget_usd: 0 }\nedges:\n  - a -> b\n"
        )
    v = _run(cli, "zero", text)
    b = next(x for x in v["executions"] if x["node_id"] == "b")
    assert b["status"] == "needs-review" and b["reason"] == "budget_exceeded"
    assert b["detail"]["scope"] == "call" and b["cost_usd"] == 0
    assert b["attempt"] == 1 and "attachments" not in b["detail"]  # no call was made


# -- tools ------------------------------------------------------------------------------------


@pytest.fixture
def claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n')
    wrapper.chmod(0o755)
    log = tmp_path / "claude-argv.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    return str(wrapper), log


def _claude_pipeline(name: str, bin: str, extra: str) -> str:
    return (
        f"version: 1\nname: {name}\nnodes:\n"
        "  a:\n    type: agent\n    runner: claude-code\n    prompt: 'do it mode:json'\n"
        f"    out: x\n{extra}"
        f"    with: {{ bin: {json.dumps(bin)}, env: [ANTHROPIC_API_KEY, FAKE_CLAUDE_LOG] }}\n"
        "edges: []\n"
    )


def _tools(argv: list[str]) -> list[str]:
    return argv[argv.index("--allowedTools") + 1].split(",")


def test_default_tools_are_read_only(cli: Cli, claude: tuple[str, Path]) -> None:
    bin, log = claude
    v = _run(cli, "reader", _claude_pipeline("reader", bin, ""))
    assert v["run"]["status"] == "done"
    assert _tools(json.loads(log.read_text())["argv"]) == ["Read", "Grep", "Glob"]


def test_allowed_tools_reach_the_cli(cli: Cli, claude: tuple[str, Path]) -> None:
    bin, log = claude
    extra = "    allowed_tools: [read, edit, bash]\n    workspace: { type: fakebox, repo: app }\n"
    v = _run(cli, "writer", _claude_pipeline("writer", bin, extra))
    assert v["run"]["status"] == "done", v
    assert _tools(json.loads(log.read_text())["argv"]) == [
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "MultiEdit",
        "Bash",
    ]


@pytest.mark.parametrize("tools", ["[read, edit]", "[bash]"])
def test_write_tools_without_workspace_are_rejected(
    cli: Cli, claude: tuple[str, Path], tools: str
) -> None:
    bin, log = claude
    path = cli.root / "unsafe.yaml"
    path.write_text(_claude_pipeline("unsafe", bin, f"    allowed_tools: {tools}\n"))
    code, out, _ = cli.run("validate", str(path))
    assert code == 1
    assert "nodes.a.allowed_tools: agent with" in out
    assert "has no workspace, so its edits would land in the agentline host's" in out
    assert "(hint: add workspace: { type: container" in out
    code, _, err = cli.run("run", str(path))
    assert code == 1 and "has no workspace" in err
    assert not log.exists()  # never ran
