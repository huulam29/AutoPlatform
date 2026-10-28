"""aider runner against a fake `aider` executable."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentline.errors import BudgetExceeded, PermanentError
from agentline.registry import Limits, RunRequest
from agentline.runners import AiderRunner, RunnerError
from agentline.runners.aider import build_argv, parse_output, tool_flags
from agentline.runners.common import run_process

FAKE = Path(__file__).with_name("fake_aider.py")


@pytest.fixture
def aider_bin(tmp_path: Path) -> str:
    wrapper = tmp_path / "aider"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_AGENT_LOG", str(path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("SOME_OTHER_SECRET", "must-not-leak")
    return path


def request(prompt: str, bin: str, **kw: Any) -> RunRequest:
    params: dict[str, Any] = {"bin": bin, "env": ["OPENAI_API_KEY", "FAKE_AGENT_LOG"]}
    params.update(kw.pop("params", {}))
    return RunRequest(
        prompt=prompt,
        inputs={},
        limits=kw.pop("limits", Limits(allowed_tools=("read", "edit", "bash"))),
        params=params,
        **kw,
    )


def run(req: RunRequest) -> Any:
    return asyncio.run(AiderRunner().run(req))


def test_tool_flags() -> None:
    assert tool_flags(("read",)) == [
        "--chat-mode",
        "ask",
        "--dry-run",
        "--no-suggest-shell-commands",
        "--no-detect-urls",
    ]
    assert tool_flags(("read", "edit")) == ["--no-suggest-shell-commands", "--no-detect-urls"]
    assert tool_flags(("read", "edit", "bash", "web")) == []


def test_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_AIDER_BIN", "/opt/aider")
    req = RunRequest(
        "fix it", {}, Limits(allowed_tools=("read", "edit")), params={"model": "sonnet"}
    )
    argv = build_argv(req)
    assert argv[:3] == ["/opt/aider", "--message", "fix it"]
    assert "--yes-always" in argv and "--no-gitignore" in argv
    assert "--no-auto-commits" in argv and "--no-dirty-commits" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--chat-history-file") + 1] == "/dev/null"
    assert argv[-2:] == ["--no-suggest-shell-commands", "--no-detect-urls"]
    req.params["auto_commits"] = True
    assert "--auto-commits" in build_argv(req) and "--no-auto-commits" not in build_argv(req)
    req.params["args"] = ["--yes", "--model", "{model}"]
    assert build_argv(req) == ["/opt/aider", "--yes", "--model", "sonnet", "--message", "fix it"]


def test_parse_output() -> None:
    data = parse_output(
        "hello\nTokens: 2.3k sent, 120 received. Cost: $0.01 message, $0.01 session.\n"
        "Tokens: 3.1k sent, 1.2k cache hit, 80 received. Cost: $0.02 message, $1,000.50 session.\n"
    )
    assert data["cost"] == pytest.approx(1000.5)
    assert data["usage"] == {"input_tokens": 5400, "output_tokens": 200}
    assert data["text"] == "hello"


def test_json_result_cost_and_env(aider_bin: str, log: Path) -> None:
    res = run(request("do it", aider_bin))
    assert res.output == {"summary": "done", "files": ["a.py"]}
    assert res.cost_usd == pytest.approx(0.03)
    assert res.meta["usage"]["input_tokens"] == 5400
    seen = json.loads(log.read_text())
    assert seen["argv"][:2] == ["--message", "do it"]
    assert seen["has_api_key"]
    assert "SOME_OTHER_SECRET" not in seen["env_keys"]


def test_text_and_no_cost_warning(aider_bin: str, log: Path) -> None:
    assert "I changed the login handler." in run(request("mode:text", aider_bin)).output
    res = run(request("mode:nocost", aider_bin))
    assert res.cost_usd == 0.0 and "no cost" in res.meta["warnings"][0]


def test_errors(aider_bin: str, log: Path) -> None:
    with pytest.raises(RunnerError, match="AuthenticationError") as ei:
        run(request("mode:autherror", aider_bin))
    assert "Aider v0.86.0" in ei.value.transcript
    with pytest.raises(RunnerError, match=r"(?s)exit 1.*boom"):
        run(request("mode:crash", aider_bin))


def test_missing_tools(aider_bin: str, log: Path) -> None:
    res = run(request("mode:missing", aider_bin))
    assert res.output is None and res.missing_tools == ["psql"]
    assert res.cost_usd == pytest.approx(0.03)


def test_budget(aider_bin: str, log: Path) -> None:
    with pytest.raises(BudgetExceeded):
        run(request("x", aider_bin, limits=Limits(budget_usd=0.0)))
    res = run(request("x", aider_bin, limits=Limits(budget_usd=0.01)))
    assert res.meta["budget_exceeded"] == {"cost_usd": 0.03, "budget_usd": 0.01}


def test_timeout(aider_bin: str, log: Path) -> None:
    with pytest.raises(TimeoutError):
        run(request("mode:sleep", aider_bin, limits=Limits(timeout=0.5)))


def test_missing_binary_is_permanent(tmp_path: Path) -> None:
    with pytest.raises(PermanentError, match="not found"):
        run(request("x", str(tmp_path / "nope")))


class DirWorkspace:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[list[str]] = []

    async def exec(
        self, cmd: list[str], *, env: dict[str, str] | None = None, timeout: float | None = None
    ) -> tuple[int, str, str]:
        self.calls.append(cmd)
        return await run_process(cmd, cwd=self.path, env=env, timeout=timeout)


def test_runs_inside_git_workspace_and_excludes_aider_files(
    aider_bin: str, log: Path, tmp_path: Path
) -> None:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    subprocess.run(["git", "init", "-q", str(ws_dir)], check=True)
    ws = DirWorkspace(ws_dir)
    res = run(request("do it", aider_bin, workspace=ws))
    assert res.output["summary"] == "done"
    assert ws.calls[0][:2] == ["sh", "-c"] and ws.calls[1][0] == aider_bin
    assert Path(json.loads(log.read_text())["cwd"]).resolve() == ws_dir.resolve()
    exclude = ws_dir / ".git" / "info" / "exclude"
    assert exclude.read_text().splitlines().count(".aider*") == 1
    run(request("do it", aider_bin, workspace=ws))  # idempotent
    assert exclude.read_text().splitlines().count(".aider*") == 1
    (ws_dir / ".aider.tags.cache.v4").mkdir()
    (ws_dir / ".aider.tags.cache.v4" / "x").write_text("cache")
    status = subprocess.run(
        ["git", "-C", str(ws_dir), "status", "--porcelain"], capture_output=True, text=True
    )
    assert ".aider" not in status.stdout


def test_exclude_is_best_effort(aider_bin: str, log: Path, tmp_path: Path) -> None:
    ws = DirWorkspace(tmp_path)  # not a git repo: the exclude step fails quietly
    assert run(request("do it", aider_bin, workspace=ws)).output["summary"] == "done"
    ws2 = DirWorkspace(tmp_path)
    run(request("do it", aider_bin, workspace=ws2, params={"exclude_aider_files": False}))
    assert ws2.calls[0][0] == aider_bin
