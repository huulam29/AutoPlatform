"""codex runner against a fake `codex` executable."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentline.builtins import default_registry
from agentline.errors import BudgetExceeded, PermanentError
from agentline.registry import Limits, RunRequest
from agentline.runners import CodexRunner, RunnerError
from agentline.runners.agent_cli import render_args
from agentline.runners.codex import build_argv, parse_events
from agentline.runners.common import run_process

FAKE = Path(__file__).with_name("fake_codex.py")
PRICES = {"input_usd_per_mtok": 2.0, "output_usd_per_mtok": 10.0}


@pytest.fixture
def codex_bin(tmp_path: Path) -> str:
    wrapper = tmp_path / "codex"
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
    params: dict[str, Any] = {"bin": bin, "env": ["OPENAI_API_KEY", "FAKE_AGENT_LOG"], **PRICES}
    params.update(kw.pop("params", {}))
    return RunRequest(
        prompt=prompt,
        inputs={},
        limits=kw.pop("limits", Limits(allowed_tools=("read", "edit", "bash"))),
        params=params,
        **kw,
    )


def run(req: RunRequest, pricing: dict[str, Any] | None = None) -> Any:
    return asyncio.run(CodexRunner(pricing).run(req))


def test_registered() -> None:
    reg = default_registry()
    assert {"codex", "aider", "claude-code"} <= set(reg.runners)


def test_argv_and_tool_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_CODEX_BIN", "/opt/codex")
    req = RunRequest("hi", {}, Limits(allowed_tools=("read",)), params={"model": "gpt-5"})
    assert build_argv(req) == [
        "/opt/codex",
        "exec",
        "--json",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5",
        "--skip-git-repo-check",
        "--",
        "hi",
    ]
    req = RunRequest("hi", {}, Limits(allowed_tools=("read", "edit", "web")))
    argv = build_argv(req)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[5:7] == ["-c", "tools.web_search=true"]
    req.params["sandbox"] = "danger-full-access"
    req.params["extra_args"] = ["--profile", "ci"]
    argv = build_argv(req)
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert argv[-4:] == ["--profile", "ci", "--", "hi"]


def test_custom_args_replace_defaults() -> None:
    req = RunRequest(
        "do it",
        {},
        Limits(allowed_tools=("read",)),
        params={"bin": "codex", "args": ["exec", "--full-auto", "--model", "{model}", "{prompt}"]},
    )
    assert build_argv(req) == ["codex", "exec", "--full-auto", "do it"]  # no model: pair dropped
    req.params["model"] = "o4"
    req.params["args"] = ["e", "-m", "{model}", "--sandbox={sandbox}"]
    assert build_argv(req) == ["codex", "e", "-m", "o4", "--sandbox=read-only", "do it"]


def test_render_args_keeps_prompt_whole() -> None:
    args, placed = render_args(
        ["-p", "{prompt}", "x{model}"], {"prompt": "{model} a b", "model": "m"}
    )
    assert args == ["-p", "{model} a b", "xm"] and placed


def test_json_result_cost_and_env(codex_bin: str, log: Path) -> None:
    res = run(request("do it", codex_bin))
    assert res.output == {"summary": "done", "files": ["a.py"]}
    assert res.meta["thread_id"] == "th-1"
    assert res.meta["usage"] == {
        "input_tokens": 1000,
        "cached_input_tokens": 200,
        "output_tokens": 100,
    }
    assert res.cost_usd == pytest.approx((1000 * 2.0 + 100 * 10.0) / 1e6)
    seen = json.loads(log.read_text())
    assert seen["argv"][:2] == ["exec", "--json"]
    assert seen["argv"][-2:] == ["--", "do it"]
    assert seen["has_api_key"]
    assert "SOME_OTHER_SECRET" not in seen["env_keys"]


def test_pricing_table_and_unknown_price(codex_bin: str, log: Path) -> None:
    req = request("mode:text", codex_bin, params={"model": "gpt-5"})
    for k in PRICES:
        del req.params[k]
    res = run(req, {"gpt-5": {"input": 1.0, "output": 2.0}})
    assert res.output == "I changed the login handler."
    assert res.cost_usd == pytest.approx((1000 * 1.0 + 100 * 2.0) / 1e6)
    res = run(req)
    assert res.cost_usd == 0.0 and "no price known" in res.meta["warnings"][0]


def test_legacy_event_format(codex_bin: str, log: Path) -> None:
    res = run(request("mode:legacy", codex_bin))
    assert res.output == "legacy ok"
    assert res.meta["usage"]["input_tokens"] == 1000


def test_error_raises_with_cost(codex_bin: str, log: Path) -> None:
    with pytest.raises(RunnerError, match="stream disconnected") as ei:
        run(request("mode:error", codex_bin))
    assert ei.value.cost_usd == pytest.approx(2000 * 2.0 / 1e6)
    assert "turn.failed" in ei.value.transcript


def test_garbage_output_is_retryable(codex_bin: str, log: Path) -> None:
    with pytest.raises(RunnerError, match=r"without JSON events.*unknown argument"):
        run(request("mode:garbage", codex_bin))


def test_missing_tools(codex_bin: str, log: Path) -> None:
    res = run(request("mode:missing", codex_bin))
    assert res.output is None
    assert res.missing_tools == ["protoc", "psql"]


def test_budget(codex_bin: str, log: Path) -> None:
    with pytest.raises(BudgetExceeded):
        run(request("x", codex_bin, limits=Limits(budget_usd=0.0)))
    res = run(request("mode:expensive", codex_bin, limits=Limits(budget_usd=1.0)))
    assert res.cost_usd > 1.0 and res.meta["budget_exceeded"]["budget_usd"] == 1.0


def test_timeout(codex_bin: str, log: Path) -> None:
    with pytest.raises(TimeoutError):
        run(request("mode:sleep", codex_bin, limits=Limits(timeout=0.5)))


def test_missing_binary_is_permanent(tmp_path: Path) -> None:
    with pytest.raises(PermanentError, match="not found"):
        run(request("x", str(tmp_path / "nope")))


def test_parse_events_ignores_noise() -> None:
    data = parse_events('not json\n{"type":"turn.started"}\n[1,2]\n')
    assert data["events"] == 1 and data["result"] == ""


class DirWorkspace:
    def __init__(self, path: Path, missing: bool = False) -> None:
        self.path = path
        self.missing = missing
        self.calls: list[list[str]] = []

    async def exec(
        self, cmd: list[str], *, env: dict[str, str] | None = None, timeout: float | None = None
    ) -> tuple[int, str, str]:
        self.calls.append(cmd)
        if self.missing:
            return 127, "", f'exec: "{cmd[0]}": executable file not found in $PATH: not found'
        return await run_process(cmd, cwd=self.path, env=env, timeout=timeout)


def test_runs_inside_workspace(codex_bin: str, log: Path, tmp_path: Path) -> None:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    ws = DirWorkspace(ws_dir)
    res = run(request("do it", codex_bin, workspace=ws))
    assert res.output["summary"] == "done"
    assert ws.calls[0][0] == codex_bin
    assert "--skip-git-repo-check" not in ws.calls[0]
    assert Path(json.loads(log.read_text())["cwd"]).resolve() == ws_dir.resolve()


def test_binary_missing_in_workspace_is_a_missing_tool(tmp_path: Path) -> None:
    ws = DirWorkspace(tmp_path, missing=True)
    res = run(request("x", "/usr/local/bin/codex", workspace=ws))
    assert res.missing_tools == ["codex"]
