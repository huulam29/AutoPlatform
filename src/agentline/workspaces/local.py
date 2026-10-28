"""Provider `local`: a git worktree from the mirror, no container. For development and tests;
it gives the agent no isolation beyond its own checkout.

Options (`workspace: { type: local, ... }`): `repo`, `base` (branch to start from),
`secrets` (host env var names passed to commands), `autocommit`, `keep_worktree`.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

from agentline.config import Config, RepoConfig
from agentline.registry import Workspace, WorkspaceSpec
from agentline.runners.common import GIT_IDENTITY, base_env, declared_env, run_process
from agentline.workspaces import git
from agentline.workspaces.base import GitWorkspace, checkout

if TYPE_CHECKING:
    from agentline.context import StepContext


class LocalWorkspace(GitWorkspace):
    def __init__(
        self, ctx: StepContext, spec: WorkspaceSpec, repo: RepoConfig, wt: git.Worktree
    ) -> None:
        super().__init__(ctx, spec, repo, wt)
        self.path = wt.path
        self.secrets = declared_env(spec.options.get("secrets") or ())

    async def exec(
        self,
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        input: str | None = None,
    ) -> tuple[int, str, str]:
        full = {**base_env(), **GIT_IDENTITY, **self.secrets, **(env or {})}
        try:
            return await run_process(cmd, cwd=self.path, env=full, input=input, timeout=timeout)
        except FileNotFoundError:
            return 127, "", f"{cmd[0]}: command not found\n"


class LocalProvider:
    name = "local"

    def __init__(self, config: Config | None = None, *, home: Path | None = None) -> None:
        self.config = config
        self.home = home

    @contextlib.asynccontextmanager
    async def _acquire(self, ctx: StepContext, spec: WorkspaceSpec) -> AsyncIterator[Workspace]:
        async with checkout(ctx, spec, self.config, self.home) as (repo, wt):
            yield LocalWorkspace(ctx, spec, repo, wt)

    def acquire(
        self, ctx: StepContext, spec: WorkspaceSpec
    ) -> contextlib.AbstractAsyncContextManager[Workspace]:
        return self._acquire(ctx, spec)
