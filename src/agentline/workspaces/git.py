"""Bare mirrors (decision 4) and per-execution worktrees on `agentline/<run_id>`.

Layout under `$AGENTLINE_HOME` (default `~/.agentline`):
- `mirrors/<repo>.git`   `git clone --mirror` of the remote, fetched before each use.
- `worktrees/<repo>/<execution id>`   one worktree per node execution, removed afterwards.

After the mirror clone the mirror is re-configured so it's safe to commit into:
`remote.origin.mirror` is unset (a plain `git push origin <branch>` must never mirror-push
every ref), and the fetch refspec becomes `+refs/heads/*:refs/heads/*`,
`^refs/heads/agentline/*`, `+refs/tags/*:refs/tags/*` without `--prune`, so a fetch never
overwrites or deletes the local `agentline/*` branches the agents commit to.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agentline.config import RepoConfig
from agentline.runners.common import GIT_IDENTITY, base_env, run_process

BRANCH_PREFIX = "agentline/"
FETCH_REFSPECS = (
    "+refs/heads/*:refs/heads/*",
    f"^refs/heads/{BRANCH_PREFIX}*",
    "+refs/tags/*:refs/tags/*",
)
_locks: dict[str, asyncio.Lock] = {}


class GitError(RuntimeError):
    pass


def agentline_home() -> Path:
    return Path(os.environ.get("AGENTLINE_HOME") or Path.home() / ".agentline")


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "repo"


async def git(*args: str, cwd: Path | None = None, timeout: float | None = 600) -> str:
    env = {**base_env(), **GIT_IDENTITY, "GIT_TERMINAL_PROMPT": "0"}
    code, out, err = await run_process(["git", *args], cwd=cwd, env=env, timeout=timeout)
    if code != 0:
        raise GitError(f"git {' '.join(args[:3])} failed ({code}): {err.strip()[-500:]}")
    return out


async def ensure_mirror(repo: RepoConfig, root: Path | None = None) -> Path:
    """Clone the repo as a mirror on first use, else fetch it. Returns the mirror path."""
    root = root or agentline_home() / "mirrors"
    path = root / f"{safe_name(repo.name)}.git"
    lock = _locks.setdefault(str(path), asyncio.Lock())
    async with lock:
        if not (path / "HEAD").exists():
            root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            if tmp.exists():
                await _rmtree(tmp)
            await git("clone", "--mirror", "--quiet", repo.remote, str(tmp))
            await git("config", "--unset", "remote.origin.mirror", cwd=tmp)
            await git("config", "--unset-all", "remote.origin.fetch", cwd=tmp)
            for spec in FETCH_REFSPECS:
                await git("config", "--add", "remote.origin.fetch", spec, cwd=tmp)
            tmp.rename(path)  # atomic: a crash mid-clone never leaves a half mirror
        else:
            await git("fetch", "--quiet", "origin", cwd=path)
    return path


@dataclass
class Worktree:
    mirror: Path
    path: Path
    branch: str
    base_sha: str
    start_sha: str  # head when this execution started (== base_sha on iteration 1)
    created_branch: bool


def branch_for(run_id: str) -> str:
    return f"{BRANCH_PREFIX}{run_id}"


async def ref_sha(mirror: Path, ref: str) -> str | None:
    try:
        out = await git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=mirror)
        return out.strip()
    except GitError:
        return None


async def create_worktree(
    mirror: Path, path: Path, run_id: str, base_ref: str = "main"
) -> Worktree:
    """Worktree on `agentline/<run_id>`: new from `base_ref` the first time, else continuing
    the branch from its current head (iteration 2+, or a retry)."""
    branch = branch_for(run_id)
    await git("worktree", "prune", cwd=mirror)
    await _remove_stale(mirror, branch)
    if path.exists():
        await _rmtree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = await ref_sha(mirror, f"refs/heads/{base_ref}") or await ref_sha(mirror, "HEAD")
    if base is None:
        raise GitError(f"{mirror}: no {base_ref!r} branch and no HEAD to start from")
    existing = await ref_sha(mirror, f"refs/heads/{branch}")
    if existing is None:
        await git("worktree", "add", "--quiet", "-b", branch, str(path), base, cwd=mirror)
        start, base_sha, created = base, base, True
    else:
        await git("worktree", "add", "--quiet", str(path), branch, cwd=mirror)
        start, created = existing, False
        base_sha = (await git("merge-base", base, existing, cwd=mirror)).strip() or base
    return Worktree(mirror, path, branch, base_sha, start, created)


async def head_sha(path: Path) -> str:
    return (await git("rev-parse", "HEAD", cwd=path)).strip()


async def commit_leftovers(path: Path, message: str) -> bool:
    """Commit uncommitted changes the agent left behind. True if a commit was made."""
    status = await git("status", "--porcelain", cwd=path)
    if not status.strip():
        return False
    await git("add", "-A", cwd=path)
    await git("commit", "--quiet", "--no-verify", "-m", message, cwd=path)
    return True


async def diff(path: Path, base: str, head: str) -> str:
    return await git("diff", f"{base}..{head}", cwd=path)


async def remove_worktree(wt: Worktree) -> None:
    """Remove the checkout, keep the branch (it holds the agent's commits)."""
    try:
        await git("worktree", "remove", "--force", str(wt.path), cwd=wt.mirror)
    except GitError:
        if wt.path.exists():
            await _rmtree(wt.path)
        await git("worktree", "prune", cwd=wt.mirror)


async def _remove_stale(mirror: Path, branch: str) -> None:
    """A crashed earlier attempt may still have this branch checked out; free it."""
    listing = await git("worktree", "list", "--porcelain", cwd=mirror)
    current: str | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree ") :]
        elif line == f"branch refs/heads/{branch}" and current:
            await git("worktree", "remove", "--force", current, cwd=mirror)


async def _rmtree(path: Path) -> None:
    import shutil

    await asyncio.to_thread(shutil.rmtree, path, True)
