"""shell.run: run a command on the agentline host."""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentline.actions._util import pass_env_names, require, run_cmd, tail
from agentline.context import StepContext
from agentline.errors import AgentlineError, PermanentError
from agentline.registry import ActionCall


class CommandFailed(AgentlineError):
    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


class PermanentCommandFailed(CommandFailed, PermanentError):
    pass


@dataclass
class ShellRun:
    """Run `cmd` (a string goes through /bin/sh; a list is exec'd as-is).

    Effect strategy: **none** by default (no key, no check). The effect is named
    `run#<attempt>`, so a command that *finished* (any exit code) is recorded and a retry
    attempt runs it again, while a crash *during* the command (the same attempt, reclaimed)
    leaves an intent whose outcome is unknown → `on_unknown` (default `halt`). With
    `with: { idempotent: true }` the default becomes `on_unknown: retry`.

    Params: `cmd`, `cwd`, `env` (values to set), `pass_env` (host variables the command may
    see, by name: only these and PATH/HOME/locale basics reach it), `timeout` (seconds),
    `idempotent`, `permanent` (a non-zero exit fails without retries), `output: json` (stdout
    is one JSON value: a dict is merged into the result, anything else goes under `value`).
    The command gets `AGENTLINE_INPUTS`: the path of a JSON file with the node's inputs.
    Returns `{exit_code, stdout_tail, stderr_tail, ...}`; a non-zero exit raises.
    """

    name: str = "shell.run"
    deploy_class: bool = False

    async def __call__(self, ctx: StepContext, call: ActionCall) -> dict[str, Any]:
        p = call.params
        cmd = require(p, "cmd", self.name)
        if not isinstance(cmd, (str, list)):
            raise PermanentError("shell.run: `cmd` must be a string or a list of strings")
        env = {str(k): str(v) for k, v in (p.get("env") or {}).items()}
        pass_env = pass_env_names(p.get("pass_env"), self.name)
        timeout = float(p["timeout"]) if p.get("timeout") is not None else None
        default = "retry" if p.get("idempotent") else "halt"

        want_json = p.get("output") == "json"

        async def do() -> dict[str, Any]:
            with tempfile.NamedTemporaryFile(
                "w", prefix="agentline-inputs-", suffix=".json", delete=False
            ) as f:
                json.dump(call.inputs, f, default=str)
            try:
                code, out, err = await run_cmd(
                    cmd if isinstance(cmd, str) else [str(c) for c in cmd],
                    cwd=p.get("cwd"),
                    env={**env, "AGENTLINE_INPUTS": f.name},
                    pass_env=pass_env,
                    timeout=timeout,
                )
            finally:
                Path(f.name).unlink(missing_ok=True)
            result: dict[str, Any] = {
                "exit_code": code,
                "stdout_tail": tail(out),
                "stderr_tail": tail(err),
            }
            if want_json and code == 0:
                try:
                    value = json.loads(out)
                except ValueError:
                    raise PermanentError(
                        f"shell.run: output is json but stdout isn't JSON: {out.strip()[:200]!r}"
                    ) from None
                if isinstance(value, dict):
                    result.update({k: v for k, v in value.items() if k not in result})
                else:
                    result["value"] = value
            return result

        result = await ctx.effect(
            f"run#{ctx.attempt_key}",
            do,
            key=call.effect.key,
            on_unknown=call.effect.on_unknown or default,
            description=f"shell command {str(cmd)[:200]!r}",
        )
        if result["exit_code"] != 0:
            cls = PermanentCommandFailed if p.get("permanent") else CommandFailed
            last = (result["stderr_tail"] or result["stdout_tail"]).strip()[-500:]
            raise cls(f"command exited with {result['exit_code']}: {last}", result)
        return result
