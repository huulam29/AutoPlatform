"""Shared helpers for built-in actions: subprocesses, temp worktrees, config lookups."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from agentline.config import Config
from agentline.errors import AgentlineError, PermanentError, ValidationError
from agentline.integrations.http import redact
from agentline.registry import ActionCall

TAIL_CHARS = 4000
SECRET_ENV_VARS = ("GITHUB_TOKEN", "JIRA_API_TOKEN", "GITHUB_WEBHOOK_SECRET", "JIRA_WEBHOOK_SECRET")


class CommandTimeout(AgentlineError):
    pass


def secret_values() -> list[str]:
    return [v for k in SECRET_ENV_VARS if (v := os.environ.get(k))]


def scrub(text: str) -> str:
    return redact(text, secret_values())


def tail(text: str, n: int = TAIL_CHARS) -> str:
    return scrub(text[-n:])


async def run_cmd(
    cmd: str | list[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[int, str, str]:
    """Run a command (a string goes through /bin/sh). Kills the process group on timeout."""
    full_env = {**os.environ, **(env or {})}
    kw: dict[str, Any] = {
        "cwd": str(cwd) if cwd else None,
        "env": full_env,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "start_new_session": True,
    }
    if isinstance(cmd, str):
        proc = await asyncio.create_subprocess_shell(cmd, **kw)
    else:
        proc = await asyncio.create_subprocess_exec(*cmd, **kw)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        await _kill(proc)
        raise CommandTimeout(f"command timed out after {timeout:g}s") from None
    except asyncio.CancelledError:
        await _kill(proc)
        raise
    assert proc.returncode is not None
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await proc.wait()


async def git(*args: str, cwd: str | Path | None = None, timeout: float = 300.0) -> str:
    code, out, err = await run_cmd(["git", *args], cwd=cwd, timeout=timeout)
    if code != 0:
        raise AgentlineError(f"git {' '.join(args[:3])} failed ({code}): {tail(err, 1000)}")
    return out


@contextlib.asynccontextmanager
async def sha_worktree(mirror: str | Path, sha: str) -> AsyncIterator[Path]:
    """A detached throwaway worktree of `sha` from a (bare) mirror."""
    tmp = Path(tempfile.mkdtemp(prefix="agentline-wt-"))
    path = tmp / "wt"
    await git("-C", str(mirror), "worktree", "add", "--detach", str(path), sha)
    try:
        yield path
    finally:
        with contextlib.suppress(Exception):
            await git("-C", str(mirror), "worktree", "remove", "--force", str(path))
        shutil.rmtree(tmp, ignore_errors=True)
        with contextlib.suppress(Exception):
            await git("-C", str(mirror), "worktree", "prune")


def config_of(call: ActionCall, fallback: Config | None) -> Config:
    return call.config or fallback or Config()


def env_section(config: Config, env: str) -> dict[str, Any]:
    """Raw `[environments.<env>]` table (for keys Config doesn't model: cwd, check)."""
    try:
        config.environment(env)
    except ValidationError as e:
        raise PermanentError(str(e)) from None
    return dict(config.raw.get("environments", {}).get(env, {}))


def repo_section(config: Config, name: str) -> dict[str, Any]:
    return dict(config.raw.get("repos", {}).get(name, {}))


def require(params: Mapping[str, Any], name: str, action: str) -> Any:
    value = params.get(name)
    if value in (None, ""):
        raise PermanentError(f"{action}: missing required parameter {name!r}")
    return value
