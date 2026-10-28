"""Manual smoke tests against the real `claude` CLI and a real docker daemon.

Skipped unless AGENTLINE_SMOKE=1 (and skipped if the tool is unavailable). See docs/smoke.md.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentline.config import Config
from agentline.registry import Limits, RunRequest, WorkspaceSpec
from agentline.runners import ClaudeCodeRunner, ShellRunner
from agentline.runners.claude_code import claude_bin
from agentline.workspaces import ContainerProvider, finalize_output

from ..workspaces.helpers import ctx, make_remote

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENTLINE_SMOKE") != "1", reason="set AGENTLINE_SMOKE=1 to run smoke tests"
)


def test_claude_code_real_run() -> None:
    binary = claude_bin({})
    if shutil.which(binary) is None:
        pytest.skip(f"{binary!r} is not installed")
    req = RunRequest(
        prompt='Reply with exactly this JSON and nothing else: {"ok": true}',
        inputs={},
        limits=Limits(allowed_tools=("read",), max_turns=2, timeout=180, budget_usd=0.5),
        params={"model": os.environ.get("AGENTLINE_SMOKE_MODEL") or None},
    )
    res = asyncio.run(ClaudeCodeRunner().run(req))
    print("cost", res.cost_usd, "meta", res.meta)
    assert res.output == {"ok": True}
    assert res.meta["session_id"] and res.cost_usd >= 0


def _docker() -> str:
    return os.environ.get("AGENTLINE_DOCKER") or "docker"


def test_container_workspace_real_docker(tmp_path: Path) -> None:
    docker = _docker()
    if shutil.which(docker) is None:
        pytest.skip("docker CLI not installed")
    if subprocess.run([docker, "info"], capture_output=True).returncode != 0:
        pytest.skip("docker daemon is not running")
    remote = make_remote(tmp_path / "remote")
    config = Config.from_dict({"repos": {"app": {"remote": str(remote)}}})
    provider = ContainerProvider(config, home=tmp_path / "home")
    image = os.environ.get("AGENTLINE_SMOKE_IMAGE") or "alpine:3.20"
    c = ctx(exec_id="smoke1")

    async def main() -> Any:
        async with provider.acquire(c, WorkspaceSpec("container", image=image)) as ws:
            res = await ShellRunner().run(
                RunRequest(
                    "hello from the prompt",
                    {},
                    Limits(timeout=60),
                    workspace=ws,
                    params={"cmd": "cat > smoke.txt && pwd && id -u && ls -A"},
                )
            )
            return res, await finalize_output(ws, res.output)

    res, branch = asyncio.run(main())
    stdout = res.output["stdout"].split()
    assert stdout[0] == "/workspace" and stdout[1] == str(os.getuid())
    assert branch["autocommitted"] and branch["changed"]
    shown = subprocess.run(
        ["git", "show", f"{branch['sha']}:smoke.txt"],
        cwd=branch["mirror"],
        capture_output=True,
        text=True,
    )
    assert shown.stdout == "hello from the prompt"
    ps = subprocess.run(
        [docker, "ps", "-a", "-q", "--filter", "name=agentline-smoke1"],
        capture_output=True,
        text=True,
    )
    assert ps.stdout.strip() == ""  # removed in finally
