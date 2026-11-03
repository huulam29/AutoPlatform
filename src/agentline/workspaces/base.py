"""What the `local` and `container` providers share: repo selection, the worktree lifecycle,
and `finalize()` (commit leftovers, head SHA, diff attachment)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentline.config import Config, RepoConfig
from agentline.errors import PermanentError
from agentline.registry import WorkspaceSpec
from agentline.workspaces import git
from agentline.workspaces.images import detect_markers, profile_for_markers

if TYPE_CHECKING:
    from agentline.context import StepContext


def task_suffix(ctx: StepContext) -> str:
    """`-task<n>` for a for_each task's checkout, container and diff; empty otherwise."""
    return "" if getattr(ctx, "task", None) is None else f"-task{ctx.task}"


def select_repo(ctx: StepContext, spec: WorkspaceSpec, config: Config | None) -> RepoConfig:
    """The node's `workspace.repo`, else the only configured repo, else the payload's `repo`.
    Only repos configured in agentline.toml can be checked out (a webhook payload can name
    one but never supply a remote)."""
    repos = config.repos if config else {}
    name = spec.options.get("repo")
    if name is None and len(repos) == 1:
        return next(iter(repos.values()))
    if name is None and isinstance(ctx.payload, dict):
        name = ctx.payload.get("repo")
    if name is None:
        raise PermanentError(
            "workspace: no repo selected; set `workspace: { repo: <name> }`, configure exactly "
            "one [repos.<name>] in agentline.toml, or put `repo` in the run payload"
        )
    if str(name) not in repos:
        known = ", ".join(sorted(repos)) or "none configured"
        raise PermanentError(f"workspace: unknown repo {name!r} (known: {known})")
    return repos[str(name)]


@contextlib.asynccontextmanager
async def checkout(
    ctx: StepContext, spec: WorkspaceSpec, config: Config | None, home: Path | None
) -> AsyncIterator[tuple[RepoConfig, git.Worktree]]:
    """Mirror -> worktree on `agentline/<run_id>`; the worktree is removed afterwards (the
    branch is kept) unless `keep_worktree: true`. A for_each task gets its own worktree and
    branch (`-task<n>`), so parallel tasks never share a checkout."""
    repo = select_repo(ctx, spec, config)
    root = home or git.agentline_home()
    mirror = await git.ensure_mirror(repo, root / "mirrors")
    path = root / "worktrees" / git.safe_name(repo.name) / (ctx.execution.id + task_suffix(ctx))
    base_ref = str(spec.options.get("base") or repo.default_branch)
    task = getattr(ctx, "task", None)
    wt = await git.create_worktree(mirror, path, ctx.run_id, base_ref, task=task)
    note = {
        "type": spec.type,
        "repo": repo.name,
        "branch": wt.branch,
        "base_sha": wt.base_sha,
        "start_sha": wt.start_sha,
        "worktree": str(wt.path),
    }
    if task is None:
        ctx.notes["workspace"] = note
    else:  # tasks share the node's notes
        ctx.notes.setdefault("workspaces", {})[str(task)] = note
    try:
        yield repo, wt
    finally:
        if not spec.options.get("keep_worktree"):
            await asyncio.shield(git.remove_worktree(wt))


class GitWorkspace:
    """Base for workspaces backed by a worktree. Subclasses set `path` and `exec`."""

    path: Path

    def __init__(
        self, ctx: StepContext, spec: WorkspaceSpec, repo: RepoConfig, wt: git.Worktree
    ) -> None:
        self.ctx = ctx
        self.spec = spec
        self.repo = repo
        self.worktree = wt
        self.markers: list[str] = detect_markers(wt.path)
        self.image_profile: str | None = repo.image_profile or profile_for_markers(self.markers)
        self._final: dict[str, Any] | None = None

    async def finalize(self) -> dict[str, Any]:
        """The branch artifact: `{name, sha, base_sha, repo, mirror, worktree, ...}`; also
        attaches `git diff base..head` as `diff`. Uncommitted changes are committed first
        (`autocommit: false` to skip)."""
        if self._final is not None:
            return self._final
        wt = self.worktree
        committed = False
        if self.spec.options.get("autocommit", True):
            msg = f"agentline: uncommitted changes from {self.ctx.node_id}#{self.ctx.iteration}"
            committed = await git.commit_leftovers(wt.path, msg)
        head = await git.head_sha(wt.path)
        diff = await git.diff(wt.path, wt.base_sha, head)
        await self.ctx.attach("diff" + task_suffix(self.ctx).replace("-", "."), diff)
        self._final = {
            "name": wt.branch,
            "sha": head,
            "base_sha": wt.base_sha,
            "repo": self.repo.name,
            "mirror": str(wt.mirror),
            "worktree": str(wt.path),
            "start_sha": wt.start_sha,
            "changed": head != wt.start_sha,
            "autocommitted": committed,
        }
        return self._final
