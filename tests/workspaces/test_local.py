"""Mirrors, worktrees and the `local` provider against real git and temp repos."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentline.config import Config, RepoConfig
from agentline.errors import PermanentError
from agentline.registry import WorkspaceSpec
from agentline.workspaces import LocalProvider, finalize_output
from agentline.workspaces import git as wgit
from agentline.workspaces.base import select_repo

from .helpers import ctx, git, make_remote


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    return make_remote(tmp_path / "remote", {"README.md": "hello\n", "pyproject.toml": ""})


@pytest.fixture
def config(remote: Path) -> Config:
    return Config.from_dict({"repos": {"app": {"remote": str(remote)}}})


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_ensure_mirror_clones_then_fetches(remote: Path, home: Path) -> None:
    repo = RepoConfig("app", str(remote))
    mirror = asyncio.run(wgit.ensure_mirror(repo, home / "mirrors"))
    assert mirror == home / "mirrors" / "app.git"
    assert git(mirror, "rev-parse", "main") == git(remote, "rev-parse", "main")
    assert git(mirror, "config", "--get-all", "remote.origin.fetch").splitlines() == list(
        wgit.FETCH_REFSPECS
    )
    with pytest.raises(Exception):  # noqa: B017 - unset: a push must never mirror everything
        git(mirror, "config", "--get", "remote.origin.mirror")
    # our local agentline/* branch survives a fetch, even if upstream has one of that name
    git(mirror, "branch", "agentline/r1", "main")
    ours = git(mirror, "rev-parse", "agentline/r1")
    (remote / "new.txt").write_text("x")
    git(remote, "add", "-A")
    git(remote, "commit", "-q", "-m", "upstream change")
    git(remote, "branch", "agentline/r1")
    asyncio.run(wgit.ensure_mirror(repo, home / "mirrors"))
    assert git(mirror, "rev-parse", "main") == git(remote, "rev-parse", "main")
    assert git(mirror, "rev-parse", "agentline/r1") == ours


def test_mirror_root_from_agentline_home(
    remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTLINE_HOME", str(tmp_path / "ah"))
    mirror = asyncio.run(wgit.ensure_mirror(RepoConfig("app", str(remote))))
    assert mirror == tmp_path / "ah" / "mirrors" / "app.git"


def run_agent(provider: LocalProvider, c: Any, spec: WorkspaceSpec, script: str) -> Any:
    async def main() -> Any:
        async with provider.acquire(c, spec) as ws:
            code, _, err = await ws.exec(["sh", "-c", script])
            assert code == 0, err
            return await finalize_output(ws, {"done": True}), ws.path

    return asyncio.run(main())


COMMIT = "echo {text} >> work.txt && git add -A && git commit -q -m '{text}'"


def test_local_workspace_commit_finalize_and_iteration_two(
    config: Config, home: Path, remote: Path
) -> None:
    provider = LocalProvider(config, home=home)
    spec = WorkspaceSpec(type="local")
    base = git(remote, "rev-parse", "main")
    c1 = ctx(exec_id="ex1")
    out1, path1 = run_agent(provider, c1, spec, COMMIT.format(text="one"))
    mirror = home / "mirrors" / "app.git"
    assert out1["name"] == "agentline/run1" and out1["base_sha"] == base
    assert out1["repo"] == "app" and out1["mirror"] == str(mirror)
    assert out1["worktree"] == str(home / "worktrees" / "app" / "ex1")
    assert out1["done"] is True and out1["changed"] is True  # dict output merged flat
    assert out1["sha"] == git(mirror, "rev-parse", "agentline/run1") != base
    assert "+one" in c1.attachments["diff"]
    assert c1.notes["workspace"]["branch"] == "agentline/run1"
    assert not path1.exists()  # worktree removed, branch kept

    c2 = ctx(exec_id="ex2", iteration=2)
    out2, _ = run_agent(provider, c2, spec, COMMIT.format(text="two"))
    assert out2["start_sha"] == out1["sha"] and out2["base_sha"] == base
    assert git(mirror, "rev-parse", "agentline/run1~1") == out1["sha"]
    assert "+one" in c2.attachments["diff"] and "+two" in c2.attachments["diff"]


def test_output_with_name_is_merged_over_branch(config: Config, home: Path) -> None:
    async def main() -> Any:
        async with LocalProvider(config, home=home).acquire(ctx(), WorkspaceSpec("local")) as ws:
            return await finalize_output(ws, {"name": "custom", "extra": 1})

    out = asyncio.run(main())
    assert out["name"] == "custom" and out["extra"] == 1 and "sha" in out


def test_leftover_changes_are_autocommitted(config: Config, home: Path) -> None:
    provider = LocalProvider(config, home=home)
    out, _ = run_agent(provider, ctx(), WorkspaceSpec("local"), "echo dirty > left.txt")
    assert out["autocommitted"] is True and out["changed"] is True
    mirror = Path(out["mirror"])
    assert "left.txt" in git(mirror, "show", "--stat", out["sha"])
    out, _ = run_agent(
        provider,
        ctx(run_id="run2"),
        WorkspaceSpec("local", options={"autocommit": False}),
        "echo dirty > left.txt",
    )
    assert out["autocommitted"] is False and out["changed"] is False


def test_crashed_attempt_left_worktree_behind(config: Config, home: Path) -> None:
    provider = LocalProvider(config, home=home)
    keep = WorkspaceSpec("local", options={"keep_worktree": True})
    _, path1 = run_agent(provider, ctx(exec_id="ex1"), keep, COMMIT.format(text="a"))
    assert path1.exists()  # e.g. the worker died before cleanup
    out, _ = run_agent(provider, ctx(exec_id="ex1b"), WorkspaceSpec("local"), "true")
    assert not path1.exists() and out["name"] == "agentline/run1"


def test_exec_env_is_scrubbed(config: Config, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_TOKEN", "leak")
    monkeypatch.setenv("DECLARED_TOKEN", "ok")

    async def main() -> str:
        spec = WorkspaceSpec("local", options={"secrets": ["DECLARED_TOKEN"]})
        async with LocalProvider(config, home=home).acquire(ctx(), spec) as ws:
            _, out, _ = await ws.exec(["env"])
            missing = await ws.exec(["no-such-tool-zz"])
            assert missing[0] == 127 and "command not found" in missing[2]
            return out

    out = asyncio.run(main())
    assert "DECLARED_TOKEN=ok" in out and "HOST_TOKEN" not in out


def test_select_repo() -> None:
    one = Config.from_dict({"repos": {"app": {"remote": "x"}}})
    two = Config.from_dict({"repos": {"app": {"remote": "x"}, "web": {"remote": "y"}}})
    assert select_repo(ctx(), WorkspaceSpec(), one).name == "app"
    assert select_repo(ctx(), WorkspaceSpec(options={"repo": "web"}), two).name == "web"
    assert select_repo(ctx(payload={"repo": "web"}), WorkspaceSpec(), two).name == "web"
    with pytest.raises(PermanentError, match="no repo selected"):
        select_repo(ctx(), WorkspaceSpec(), two)
    with pytest.raises(PermanentError, match="unknown repo 'nope'"):
        select_repo(ctx(payload={"repo": "nope"}), WorkspaceSpec(), two)
    with pytest.raises(PermanentError):  # a payload can't supply a remote
        select_repo(ctx(payload={"repo": "/tmp/evil"}), WorkspaceSpec(), None)
