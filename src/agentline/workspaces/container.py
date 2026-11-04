"""Provider `container`: one Docker container per node execution, the worktree mounted at
`/workspace`.

    docker run -d --rm --name agentline-<execution id> -v <worktree>:/workspace
      -v <mirror>:<mirror> -w /workspace --user <uid>:<gid> --network <net>
      --cap-drop ALL --security-opt no-new-privileges [-e NAME ...] <image> sleep infinity

then every command is `docker exec`, and `docker rm -f` runs in `finally` (also on
cancellation). The mirror is mounted at its host path because a worktree's `.git` file points
there. No docker socket is mounted and no host env is passed except declared `secrets` (by
name: `-e NAME`, so values never appear in argv). `--network` defaults to `bridge`; egress
restriction (LLM API only) is future work.

Options: `repo`, `base`, `secrets`, `network`, `user` ("uid:gid"), `docker_args` (extra
`docker run` args), `autocommit`, `keep_worktree`. Docker CLI: `docker` option, else
`[docker] bin` in agentline.toml, else `$AGENTLINE_DOCKER`, else `docker`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentline.config import Config, RepoConfig
from agentline.errors import PermanentError
from agentline.registry import Workspace, WorkspaceSpec
from agentline.runners.common import GIT_IDENTITY, Executor, base_env, declared_env, run_process
from agentline.workspaces import git
from agentline.workspaces.base import GitWorkspace, checkout, task_suffix
from agentline.workspaces.images import ImageChoice, build_image, resolve_image

if TYPE_CHECKING:
    from agentline.context import StepContext

MOUNT = "/workspace"
CONTAINER_HOME = "/tmp/agentline-home"


class ContainerWorkspace(GitWorkspace):
    def __init__(
        self,
        ctx: StepContext,
        spec: WorkspaceSpec,
        repo: RepoConfig,
        wt: git.Worktree,
        *,
        container: str,
        docker: str,
        executor: Executor,
        image: ImageChoice,
    ) -> None:
        super().__init__(ctx, spec, repo, wt)
        self.path = Path(MOUNT)
        self.host_path = wt.path
        self.container = container
        self.docker = docker
        self.executor = executor
        self.image = image
        self.markers = image.markers or self.markers
        self.image_profile = image.profile or self.image_profile

    async def exec(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> tuple[int, str, str]:
        env = env or {}
        argv = [self.docker, "exec"]
        if input is not None:
            argv.append("-i")
        argv += ["-w", MOUNT]
        for k in env:
            argv += ["-e", k]  # value comes from the docker CLI's own env, never argv
        argv += [self.container, *cmd]
        return await self.executor(
            argv, env={**base_env(), **env}, input=input, timeout=timeout, cwd=None
        )


def run_argv(
    *,
    docker: str,
    name: str,
    image: str,
    worktree: Path,
    mirror: Path,
    user: str,
    network: str,
    secret_names: list[str],
    labels: dict[str, str],
    extra: list[str],
) -> list[str]:
    argv = [docker, "run", "-d", "--rm", "--name", name]
    for k, v in labels.items():
        argv += ["--label", f"{k}={v}"]
    argv += [
        "-v",
        f"{worktree}:{MOUNT}",
        "-v",
        f"{mirror}:{mirror}",
        "-w",
        MOUNT,
        "--user",
        user,
        "--network",
        network,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "-e",
        f"HOME={CONTAINER_HOME}",
    ]
    for k, v in GIT_IDENTITY.items():
        argv += ["-e", f"{k}={v}"]
    for k in secret_names:
        argv += ["-e", k]
    return [*argv, *extra, image, "sleep", "infinity"]


class ContainerProvider:
    name = "container"

    def __init__(
        self,
        config: Config | None = None,
        *,
        docker: str | None = None,
        executor: Executor | None = None,
        home: Path | None = None,
    ) -> None:
        self.config = config
        self.docker = docker
        self.executor: Executor = executor or run_process
        self.home = home

    def docker_bin(self, spec: WorkspaceSpec) -> str:
        cfg = (self.config.raw.get("docker") or {}) if self.config else {}
        return str(
            spec.options.get("docker")
            or self.docker
            or cfg.get("bin")
            or os.environ.get("AGENTLINE_DOCKER")
            or "docker"
        )

    @contextlib.asynccontextmanager
    async def _acquire(self, ctx: StepContext, spec: WorkspaceSpec) -> AsyncIterator[Workspace]:
        if sys.platform == "win32":
            # host paths are mounted as `<path>:<path>` and the uid is mapped: POSIX hosts only
            raise PermanentError(
                "container workspaces need a Linux or macOS host (run the engine in WSL);"
                " use `workspace: { type: local }` on Windows"
            )
        docker = self.docker_bin(spec)
        run = self.executor
        async with checkout(ctx, spec, self.config, self.home) as (repo, wt):
            choice = resolve_image(spec.image, repo, self.config, wt.path)
            note: dict[str, Any] = choice.note()
            if choice.needs_build:
                note.update(await build_image(choice, docker=docker, executor=run))
            ctx.notes["image"] = note
            name = f"agentline-{ctx.execution.id}{task_suffix(ctx)}"
            await run([docker, "rm", "-f", name], env=base_env(), timeout=60)  # crashed retry
            secret_names = [str(s) for s in spec.options.get("secrets") or ()]
            argv = run_argv(
                docker=docker,
                name=name,
                image=choice.ref,
                worktree=wt.path,
                mirror=wt.mirror,
                user=str(spec.options.get("user") or f"{os.getuid()}:{os.getgid()}"),
                network=str(spec.options.get("network") or "bridge"),
                secret_names=secret_names,
                labels={"agentline.run": ctx.run_id, "agentline.execution": ctx.execution.id},
                extra=[str(x) for x in spec.options.get("docker_args") or ()],
            )
            env = {**base_env(), **declared_env(secret_names)}
            try:
                code, _, err = await run(argv, env=env, timeout=300)
                if code != 0:
                    raise RuntimeError(f"docker run {choice.ref} failed ({code}): {err[-500:]}")
                ctx.notes["container"] = name
                yield ContainerWorkspace(
                    ctx,
                    spec,
                    repo,
                    wt,
                    container=name,
                    docker=docker,
                    executor=run,
                    image=choice,
                )
            finally:
                await asyncio.shield(run([docker, "rm", "-f", name], env=base_env(), timeout=60))

    def acquire(
        self, ctx: StepContext, spec: WorkspaceSpec
    ) -> contextlib.AbstractAsyncContextManager[Workspace]:
        return self._acquire(ctx, spec)
