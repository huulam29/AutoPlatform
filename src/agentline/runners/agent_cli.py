"""Shared plumbing for coding-agent CLIs run as runners (codex, aider): user-overridable
argument lists, running in the workspace or locally, and the missing-binary rules."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from agentline.errors import PermanentError
from agentline.registry import RunRequest
from agentline.runners.common import base_env, child_env, run_process, workspace_exec


class BinaryMissingInWorkspace(Exception):
    """The CLI isn't installed in the workspace (container): report it as a missing tool."""

    def __init__(self, tool: str, output: str) -> None:
        super().__init__(tool)
        self.tool = tool
        self.output = output


def render_args(template: Sequence[Any], values: dict[str, str | None]) -> tuple[list[str], bool]:
    """Fill `{name}` placeholders in a user-supplied `args:` list.

    An arg that is exactly `{prompt}` becomes the prompt (never split or re-parsed). An arg that
    is exactly a placeholder whose value is empty is dropped, together with the flag right
    before it when that one starts with `-` (so `["--model", "{model}"]` disappears when no
    model is set). Other args get every known placeholder replaced as text.
    Returns (args, whether the prompt was placed)."""
    out: list[str] = []
    placed = False
    for raw in template:
        arg = str(raw)
        key = arg[1:-1] if arg.startswith("{") and arg.endswith("}") else None
        if key is not None and key in values:
            value = values[key]
            if key == "prompt":
                placed = True
            if value in (None, ""):
                if out and out[-1].startswith("-"):
                    out.pop()
                continue
            out.append(str(value))
            continue
        for k, v in values.items():
            if k != "prompt":
                arg = arg.replace("{" + k + "}", v or "")
        out.append(arg)
    return out, placed


def cli_bin(params: dict[str, Any], env_var: str, default: str) -> str:
    return str(params.get("bin") or os.environ.get(env_var) or default)


async def exec_cli(
    request: RunRequest, argv: list[str], env_names: Sequence[str], runner: str
) -> tuple[int, str, str]:
    """Run the CLI in the workspace (when there is one) or locally with a scrubbed env:
    only PATH/HOME/locale basics plus the declared `env_names` reach it."""
    env = child_env({**request.params, "env": list(env_names)}, env_names)
    timeout = request.limits.timeout
    try:
        if request.workspace is not None:
            code, out, err = await workspace_exec(request.workspace, argv, env=env, timeout=timeout)
        else:
            code, out, err = await run_process(argv, env={**base_env(), **env}, timeout=timeout)
    except FileNotFoundError:
        raise PermanentError(f"{runner} runner: binary {argv[0]!r} not found") from None
    if code == 127 and request.workspace is not None and "not found" in (err + out):
        raise BinaryMissingInWorkspace(os.path.basename(argv[0]), err or out)
    return code, out, err


def env_names(params: dict[str, Any], default: Sequence[str]) -> list[str]:
    names = params.get("env")
    if names is None:
        return list(default)
    if isinstance(names, str):
        return [names]
    return [str(n) for n in names]
