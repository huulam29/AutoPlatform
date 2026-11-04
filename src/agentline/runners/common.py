"""Helpers shared by runners and workspaces: subprocesses, env hygiene, output parsing."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from agentline.proctree import kill_tree, resolve_argv
from agentline.registry import Workspace

# Host env vars a local subprocess may see. Everything else (tokens, keys) must be declared.
SAFE_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TERM",
    "SHELL",
    "TZ",
    # Windows: without these, child programs (git, python, cmd) can't start or find temp/home
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
)

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "agentline",
    "GIT_AUTHOR_EMAIL": "agentline@localhost",
    "GIT_COMMITTER_NAME": "agentline",
    "GIT_COMMITTER_EMAIL": "agentline@localhost",
}


class RunnerError(RuntimeError):
    """A runner call failed after it may have spent money: carries what it cost and produced
    so the engine can record the cost and keep the transcript. Retryable like RuntimeError."""

    def __init__(
        self,
        message: str,
        *,
        cost_usd: float = 0.0,
        transcript: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd
        self.transcript = transcript
        self.meta = meta or {}


class ProcessTimeout(TimeoutError):
    pass


def base_env() -> dict[str, str]:
    return {k: os.environ[k] for k in SAFE_ENV if k in os.environ}


def declared_env(names: Iterable[str]) -> dict[str, str]:
    """Values of the declared env var names that are set on the host (never logged)."""
    return {n: os.environ[n] for n in names if n in os.environ}


def child_env(params: Mapping[str, Any], default_names: Iterable[str]) -> dict[str, str]:
    """Env for an agent CLI: the declared names (`env`, else the runner's defaults), plus
    `env_map` ({child var: host var}, e.g. ANTHROPIC_API_KEY <- MY_ANTHROPIC_KEY, so a
    provider's key reaches the CLI under the name it expects) and `env_set` (literal
    values such as a base URL). Secrets are only ever read from the host env, never params."""
    names = params.get("env")
    if names is None:
        names = list(default_names)
    elif isinstance(names, str):
        names = [names]
    env = declared_env(str(n) for n in names)
    for child, host in dict(params.get("env_map") or {}).items():
        if host in os.environ:
            env[str(child)] = os.environ[str(host)]
    for k, v in dict(params.get("env_set") or {}).items():
        env[str(k)] = str(v)
    return env


async def run_process(
    argv: list[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: str | None = None,
    timeout: float | None = None,
) -> tuple[int, str, str]:
    """Run argv; return (exit code, stdout, stderr). On timeout or cancellation the whole
    process group is killed. Raises FileNotFoundError when the binary doesn't exist."""
    proc = await asyncio.create_subprocess_exec(
        *resolve_argv(argv, dict(env) if env is not None else None),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input.encode() if input is not None else None), timeout
        )
    except (TimeoutError, asyncio.CancelledError) as e:
        kill_tree(proc.pid)
        with contextlib.suppress(Exception):
            await asyncio.shield(proc.wait())
        if isinstance(e, TimeoutError):
            raise ProcessTimeout(f"{argv[0]} timed out after {timeout:g}s") from None
        raise
    assert proc.returncode is not None
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


Executor = Callable[..., Awaitable[tuple[int, str, str]]]
"""`await executor(argv, cwd=, env=, input=, timeout=)` -> (code, stdout, stderr)."""


async def workspace_exec(
    ws: Workspace,
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    input: str | None = None,
) -> tuple[int, str, str]:
    """`ws.exec`, passing stdin only if this workspace's exec accepts `input=` (the built-in
    ones do; the Workspace protocol doesn't require it)."""
    params = inspect.signature(ws.exec).parameters
    accepts = "input" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())
    if input is not None and accepts:
        return await ws.exec(cmd, env=env, timeout=timeout, input=input)  # type: ignore[call-arg]
    return await ws.exec(cmd, env=env, timeout=timeout)


# -- output parsing ----------------------------------------------------------------------
_FENCE = re.compile(r"^```[\w-]*\s*\n(.*?)\n?```\s*$", re.S)
_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n?```", re.S)


def parse_json_text(text: str) -> tuple[bool, Any]:
    """(True, value) if the text is JSON, a fenced JSON block, or ends with a ```json block;
    else (False, text)."""
    s = text.strip()
    m = _FENCE.match(s)
    candidates = [m.group(1)] if m else [s]
    blocks = _JSON_BLOCK.findall(s)
    if blocks:
        candidates.append(blocks[-1])
    for c in candidates:
        try:
            return True, json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
    return False, text


_SH = r"(?:(?:ba|da|z)?sh: (?:line \d+: |\d+: )?)?"
_CMD = r"([A-Za-z0-9_][A-Za-z0-9_.+-]*)"
_MISSING_PATTERNS = (
    re.compile(r"^\s*MISSING_TOOL:\s*`?" + _CMD, re.M),
    re.compile(r"(?:^|[\s`'\"])" + _SH + _CMD + r": command not found(?!:)", re.M),
    re.compile(r"zsh: command not found: " + _CMD),
    re.compile(r'exec: "' + _CMD + r'": executable file not found'),
    re.compile(r"^\s*" + _SH + r"`?" + _CMD + r"`?: not found\s*$", re.M),
)
_NOT_TOOLS = {"line", "error", "file", "fatal", "warning", "path", "module"}


def detect_missing_tools(*texts: str) -> list[str]:
    """Commands reported missing: `MISSING_TOOL: x` lines and shell 'not found' errors."""
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for pat in _MISSING_PATTERNS:
            for m in pat.finditer(text):
                name = m.group(1).strip("`'\".,:")
                if name and name.lower() not in _NOT_TOOLS and name not in found:
                    found.append(name)
    return found
