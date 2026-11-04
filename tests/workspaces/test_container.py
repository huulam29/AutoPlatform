"""The `container` provider with a fake docker executor (no daemon needed): exact argv,
cleanup on error and cancellation, image build before run."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from agentline.config import Config
from agentline.registry import Limits, RunRequest, WorkspaceSpec
from agentline.runners import ShellRunner
from agentline.workspaces import ContainerProvider, image_proposal

from .helpers import FakeDocker, ctx, make_remote

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="container workspaces need a POSIX host (uid mapping, mounts)"
)


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    return make_remote(tmp_path / "remote", {"README.md": "hi\n", "package.json": "{}"})


def provider(remote: Path, tmp_path: Path, docker: FakeDocker, **cfg: Any) -> ContainerProvider:
    config = Config.from_dict({"repos": {"app": {"remote": str(remote), **cfg}}})
    return ContainerProvider(config, docker="/usr/local/bin/docker", executor=docker,
                             home=tmp_path / "home")  # fmt: skip


def test_run_exec_and_cleanup_argv(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("HOST_ONLY", "leak")
    docker = FakeDocker(have_images={"ghcr.io/acme/agent:1"})
    p = provider(remote, tmp_path, docker)
    spec = WorkspaceSpec(
        "container",
        image="ghcr.io/acme/agent:1",
        options={"secrets": ["ANTHROPIC_API_KEY"], "network": "agentline-net"},
    )
    c = ctx(exec_id="ex9")

    async def main() -> Any:
        async with p.acquire(c, spec) as ws:
            assert ws.path == Path("/workspace")
            res = await ShellRunner().run(
                RunRequest("prompt!", {}, Limits(timeout=9), workspace=ws, params={"cmd": ["ls"]})
            )
            return res, await ws.finalize()  # type: ignore[attr-defined]

    res, branch = asyncio.run(main())
    home = tmp_path / "home"
    worktree = home / "worktrees" / "app" / "ex9"
    mirror = home / "mirrors" / "app.git"
    assert res.output == {"ok": True}
    assert branch["worktree"] == str(worktree) and branch["mirror"] == str(mirror)

    d = "/usr/local/bin/docker"
    assert docker.argvs("rm") == [[d, "rm", "-f", "agentline-ex9"]] * 2  # stale + finally
    (run,) = docker.argvs("run")
    assert run == [
        d, "run", "-d", "--rm", "--name", "agentline-ex9",
        "--label", "agentline.run=run1", "--label", "agentline.execution=ex9",
        "-v", f"{worktree}:/workspace", "-v", f"{mirror}:{mirror}",
        "-w", "/workspace", "--user", f"{os.getuid()}:{os.getgid()}",
        "--network", "agentline-net", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "-e", "HOME=/tmp/agentline-home",
        "-e", "GIT_AUTHOR_NAME=agentline", "-e", "GIT_AUTHOR_EMAIL=agentline@localhost",
        "-e", "GIT_COMMITTER_NAME=agentline", "-e", "GIT_COMMITTER_EMAIL=agentline@localhost",
        "-e", "ANTHROPIC_API_KEY",
        "ghcr.io/acme/agent:1", "sleep", "infinity",
    ]  # fmt: skip
    assert "/var/run/docker.sock" not in " ".join(run)
    run_call = next(x for x in docker.calls if x["argv"][1] == "run")
    assert run_call["env"]["ANTHROPIC_API_KEY"] == "sk-test-not-real"  # via env, not argv
    assert "HOST_ONLY" not in run_call["env"]
    assert all("sk-test-not-real" not in " ".join(x["argv"]) for x in docker.calls)

    (ex,) = docker.argvs("exec")
    names = [ex[i + 1] for i, a in enumerate(ex) if a == "-e"]
    assert ex[:5] == [d, "exec", "-i", "-w", "/workspace"]
    assert ex[-2:] == ["agentline-ex9", "ls"]
    assert "AGENTLINE_PROMPT" in names and all("=" not in n for n in names)
    exec_call = next(x for x in docker.calls if x["argv"][1] == "exec")
    assert exec_call["input"] == "prompt!" and exec_call["env"]["AGENTLINE_PROMPT"] == "prompt!"
    assert c.notes["image"]["ref"] == "ghcr.io/acme/agent:1"
    assert c.notes["image"]["source"] == "explicit"


def test_container_removed_on_error_and_cancel(remote: Path, tmp_path: Path) -> None:
    docker = FakeDocker()
    p = provider(remote, tmp_path, docker, image="img:1")

    async def boom() -> None:
        async with p.acquire(ctx(exec_id="e1"), WorkspaceSpec("container")):
            raise ValueError("agent crashed")

    with pytest.raises(ValueError):
        asyncio.run(boom())
    assert docker.argvs("rm")[-1][-1] == "agentline-e1"

    started = asyncio.Event()

    async def hang() -> None:
        async with p.acquire(ctx(exec_id="e2", run_id="r2"), WorkspaceSpec("container")):
            started.set()
            await asyncio.sleep(60)

    async def cancel() -> None:
        t = asyncio.create_task(hang())
        await asyncio.wait_for(started.wait(), 10)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(cancel())
    assert docker.argvs("rm")[-1][-1] == "agentline-e2"
    assert not (tmp_path / "home" / "worktrees" / "app" / "e2").exists()


def test_auto_image_is_built_once_and_recorded(remote: Path, tmp_path: Path) -> None:
    docker = FakeDocker()
    p = provider(remote, tmp_path, docker)
    notes = []
    for i in range(2):
        c = ctx(exec_id=f"e{i}", run_id=f"r{i}")

        async def main(c: Any = c) -> None:
            async with p.acquire(c, WorkspaceSpec("container")):
                pass

        asyncio.run(main())
        notes.append(c.notes["image"])
    assert notes[0]["profile"] == "node" and notes[0]["source"] == "profile"
    assert notes[0]["ref"].startswith("agentline/img-app-") and notes[0]["built"] is True
    assert notes[1]["ref"] == notes[0]["ref"] and notes[1]["built"] is False
    (build,) = [c for c in docker.calls if c["argv"][1] == "build"]
    assert build["argv"][:6] == ["/usr/local/bin/docker", "build", "-t", notes[0]["ref"], "-f", "-"]
    assert "nodejs npm" in build["input"] and build["input"].startswith("FROM ")
    assert docker.argvs("run")[0][-3] == notes[0]["ref"]


def test_failed_docker_run_raises_and_cleans_up(remote: Path, tmp_path: Path) -> None:
    class Failing(FakeDocker):
        async def __call__(self, argv: list[str], **kw: Any) -> tuple[int, str, str]:
            if argv[1] == "run":
                self.calls.append({"argv": argv, "env": {}, "input": None})
                return 125, "", "Cannot connect to the Docker daemon"
            return await super().__call__(argv, **kw)

    docker = Failing()

    async def main() -> None:
        async with provider(remote, tmp_path, docker, image="i:1").acquire(
            ctx(), WorkspaceSpec("container")
        ):
            pass

    with pytest.raises(RuntimeError, match="Docker daemon"):
        asyncio.run(main())
    assert docker.argvs("rm")[-1][-1] == "agentline-ex1"


def test_image_proposal_uses_workspace_profile(remote: Path, tmp_path: Path) -> None:
    docker = FakeDocker()

    async def main() -> Any:
        async with provider(remote, tmp_path, docker).acquire(
            ctx(), WorkspaceSpec("container")
        ) as ws:
            return image_proposal(ws, ["psql"])

    prop = asyncio.run(main())
    assert prop["profile_from"] == "node" and prop["add_apt"] == ["postgresql-client"]
