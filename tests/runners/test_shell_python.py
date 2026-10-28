"""shell and python runners, plus the shared output-parsing helpers."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from agentline.errors import PermanentError
from agentline.registry import Limits, RunRequest, RunResult
from agentline.runners import PythonRunner, ShellRunner
from agentline.runners.common import detect_missing_tools, parse_json_text, run_process


def req(params: dict[str, Any], prompt: str = "the prompt", **kw: Any) -> RunRequest:
    limits = kw.pop("limits", Limits())
    return RunRequest(prompt, {"ticket": {"key": "P-1"}}, limits, params=params, **kw)


def shell(params: dict[str, Any], **kw: Any) -> RunResult:
    return asyncio.run(ShellRunner().run(req(params, **kw)))


# -- helpers -----------------------------------------------------------------------------
def test_parse_json_text() -> None:
    assert parse_json_text('{"a": 1}') == (True, {"a": 1})
    assert parse_json_text('```json\n{"a": 1}\n```') == (True, {"a": 1})
    assert parse_json_text("```\n[1, 2]\n```") == (True, [1, 2])
    assert parse_json_text('Here it is:\n```json\n{"a": 2}\n```\n') == (True, {"a": 2})
    assert parse_json_text("just words") == (False, "just words")


def test_detect_missing_tools() -> None:
    text = """
    $ make proto
    sh: 1: protoc: not found
    bash: psql: command not found
    zsh: command not found: jq
    OCI runtime exec failed: exec: "gcc": executable file not found in $PATH
    MISSING_TOOL: `terraform`
    Error: file not found
    The file was not found in the index.
    """
    assert sorted(detect_missing_tools(text)) == ["gcc", "jq", "protoc", "psql", "terraform"]
    assert detect_missing_tools("all good", "") == []


# -- shell -------------------------------------------------------------------------------
def test_shell_json_stdout_and_prompt_on_stdin() -> None:
    script = (
        'import json, os, sys; print(json.dumps({"stdin": sys.stdin.read(), '
        '"env": os.environ["AGENTLINE_PROMPT"], "inputs": os.environ["AGENTLINE_INPUTS"]}))'
    )
    res = shell({"cmd": [sys.executable, "-c", script]})
    assert res.output["stdin"] == "the prompt" and res.output["env"] == "the prompt"
    assert '"P-1"' in res.output["inputs"]
    assert res.meta["exit_code"] == 0


def test_shell_text_stdout_string_cmd() -> None:
    res = shell({"cmd": "echo hello; echo oops >&2"})
    assert res.output == {"stdout": "hello\n", "exit_code": 0}
    assert "oops" in res.transcript


def test_shell_nonzero_exit() -> None:
    with pytest.raises(RuntimeError, match="exited 3: bad"):
        shell({"cmd": "echo bad >&2; exit 3"})
    res = shell({"cmd": "echo partial; exit 3", "allow_failure": True})
    assert res.output == {"stdout": "partial\n", "exit_code": 3}


def test_shell_missing_tool() -> None:
    res = shell({"cmd": "definitely-not-a-tool-xyz --version"})
    assert res.missing_tools == ["definitely-not-a-tool-xyz"]
    res = shell({"cmd": ["/no/such/binary"]})
    assert res.missing_tools == ["/no/such/binary"]


def test_shell_timeout() -> None:
    with pytest.raises(TimeoutError):
        shell({"cmd": "sleep 10"}, limits=Limits(timeout=0.3))


def test_shell_env_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_SECRET", "nope")
    monkeypatch.setenv("DECLARED", "yes")
    script = "import os, json; print(json.dumps(sorted(os.environ)))"
    res = shell({"cmd": [sys.executable, "-c", script], "pass_env": ["DECLARED"]})
    assert "DECLARED" in res.output and "HOST_SECRET" not in res.output


def test_shell_bad_cmd() -> None:
    with pytest.raises(PermanentError):
        shell({})


class RecordingWorkspace:
    path = Path("/workspace")

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def exec(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> tuple[int, str, str]:
        self.calls.append({"cmd": cmd, "env": env, "timeout": timeout, "input": input})
        return 0, '{"ok": true}', ""


def test_shell_uses_workspace() -> None:
    ws = RecordingWorkspace()
    res = shell({"cmd": ["make", "test"]}, workspace=ws, limits=Limits(timeout=5))
    assert res.output == {"ok": True}
    (call,) = ws.calls
    assert call["cmd"] == ["make", "test"] and call["input"] == "the prompt"
    assert call["timeout"] == 5 and call["env"]["AGENTLINE_PROMPT"] == "the prompt"


def test_run_process_kills_on_cancel() -> None:
    async def main() -> None:
        task = asyncio.create_task(run_process(["sleep", "10"]))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(main(), 5))


# -- python ------------------------------------------------------------------------------
async def async_step(request: RunRequest) -> RunResult:
    return RunResult(output={"key": request.inputs["ticket"]["key"]}, cost_usd=0.5)


def sync_step(request: RunRequest) -> dict[str, Any]:
    return {"prompt": request.prompt}


def test_python_runner() -> None:
    r = PythonRunner()
    res = asyncio.run(r.run(req({"function": f"{__name__}:async_step"})))
    assert res.output == {"key": "P-1"} and res.cost_usd == 0.5
    res = asyncio.run(r.run(req({"function": f"{__name__}:sync_step"})))
    assert res.output == {"prompt": "the prompt"}


@pytest.mark.parametrize(
    "ref", [None, "no_colon", "no.such.module:fn", f"{__name__}:nope", f"{__name__}:__name__"]
)
def test_python_runner_bad_refs(ref: str | None) -> None:
    with pytest.raises(PermanentError):
        asyncio.run(PythonRunner().run(req({"function": ref} if ref else {})))
