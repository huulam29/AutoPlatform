"""claude-code runner against a fake `claude` executable."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agentline.errors import BudgetExceeded, PermanentError
from agentline.registry import Limits, RunRequest
from agentline.runners import ClaudeCodeRunner, RunnerError
from agentline.runners.claude_code import build_argv, map_tools
from agentline.runners.common import run_process
from tests.fakebin import fake_bin

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def claude_bin(tmp_path: Path) -> str:
    wrapper = fake_bin(tmp_path, "claude", script=FAKE)
    return str(wrapper)


def request(prompt: str, bin: str, log: Path | None = None, **kw: Any) -> RunRequest:
    params: dict[str, Any] = {"bin": bin, "env": ["ANTHROPIC_API_KEY", "FAKE_CLAUDE_LOG"]}
    params.update(kw.pop("params", {}))
    return RunRequest(
        prompt=prompt,
        inputs={},
        limits=kw.pop("limits", Limits(allowed_tools=("read", "edit", "bash"), max_turns=7)),
        params=params,
        **kw,
    )


def run(req: RunRequest) -> Any:
    return asyncio.run(ClaudeCodeRunner().run(req))


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("SOME_OTHER_SECRET", "must-not-leak")
    return path


def test_tool_mapping() -> None:
    assert map_tools(("read", "edit", "bash", "web", "mcp__x")) == [
        "Read",
        "Grep",
        "Glob",
        "Edit",
        "Write",
        "MultiEdit",
        "Bash",
        "WebFetch",
        "WebSearch",
        "mcp__x",
    ]


def test_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTLINE_CLAUDE_BIN", "/opt/claude")
    req = RunRequest(
        "hi",
        {},
        Limits(allowed_tools=("read",), max_turns=None, budget_usd=1.5),
        params={"model": "claude-opus-5-5", "append_system_prompt": "be brief"},
    )
    assert build_argv(req) == [
        "/opt/claude",
        "-p",
        "hi",
        "--output-format",
        "json",
        "--max-turns",
        "30",
        "--allowedTools",
        "Read,Grep,Glob",
        "--model",
        "claude-opus-5-5",
        "--append-system-prompt",
        "be brief",
    ]
    req.params["budget_flag"] = True
    assert build_argv(req)[-2:] == ["--max-budget-usd", "1.5000"]


def test_json_result(claude_bin: str, log: Path) -> None:
    res = run(request("do it", claude_bin))
    assert res.output == {"summary": "done", "files": ["a.py"]}
    assert res.cost_usd == pytest.approx(0.0123)
    assert res.meta["turns"] == 3 and res.meta["session_id"] == "sess-123"
    assert res.meta["model"] == "claude-sonnet-5-5"
    assert json.loads(res.transcript)["session_id"] == "sess-123"
    seen = json.loads(log.read_text())
    assert seen["argv"][:2] == ["-p", "do it"]
    assert seen["argv"][seen["argv"].index("--max-turns") + 1] == "7"
    assert "Read,Grep,Glob,Edit,Write,MultiEdit,Bash" in seen["argv"]
    assert seen["has_api_key"]
    assert "SOME_OTHER_SECRET" not in seen["env_keys"]  # only declared env vars pass


def test_text_result_and_verbose_list(claude_bin: str, log: Path) -> None:
    assert run(request("mode:text", claude_bin)).output == "I changed the login handler."
    assert run(request("mode:verbose", claude_bin)).output["summary"] == "done"


def test_is_error_raises_with_cost(claude_bin: str, log: Path) -> None:
    with pytest.raises(RunnerError, match="error_max_turns") as ei:
        run(request("mode:error", claude_bin))
    assert ei.value.cost_usd == pytest.approx(0.0123)
    assert ei.value.transcript


def test_garbage_output_is_retryable(claude_bin: str, log: Path) -> None:
    with pytest.raises(RunnerError, match=r"without a JSON result.*exploded"):
        run(request("mode:garbage", claude_bin))


def test_missing_tools(claude_bin: str, log: Path) -> None:
    res = run(request("mode:missing", claude_bin))
    assert res.missing_tools == ["protoc", "psql"]
    assert res.output is None and res.cost_usd == pytest.approx(0.0123)


def test_budget(claude_bin: str, log: Path) -> None:
    with pytest.raises(BudgetExceeded):
        run(request("x", claude_bin, limits=Limits(budget_usd=0.0)))
    res = run(request("mode:expensive", claude_bin, limits=Limits(budget_usd=1.0)))
    assert res.cost_usd == 5.0  # reported; the engine's spend() then trips the budget
    assert res.meta["budget_exceeded"] == {"cost_usd": 5.0, "budget_usd": 1.0}


def test_timeout(claude_bin: str, log: Path) -> None:
    with pytest.raises(TimeoutError):
        run(request("mode:sleep", claude_bin, limits=Limits(timeout=0.5)))


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


def test_runs_inside_workspace(claude_bin: str, log: Path, tmp_path: Path) -> None:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    ws = DirWorkspace(ws_dir)
    res = run(request("do it", claude_bin, workspace=ws))
    assert res.output["summary"] == "done"
    assert ws.calls and ws.calls[0][0] == claude_bin
    assert Path(json.loads(log.read_text())["cwd"]).resolve() == ws_dir.resolve()
