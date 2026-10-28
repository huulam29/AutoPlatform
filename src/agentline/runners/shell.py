"""Runner `shell`: run a command as the agent. `with: { cmd: [...] | "...", allow_failure,
env: { NAME: value }, pass_env: [NAME] }`.

Runs inside `request.workspace` (`workspace.exec`) when there is one, else locally. The
prompt goes in on stdin and in `AGENTLINE_PROMPT`. Output is stdout parsed as JSON when it
is JSON, else `{"stdout", "exit_code"}`. A non-zero exit raises (retryable) unless
`allow_failure: true`. Exit 126/127 with "not found" reports `missing_tools`.
"""

from __future__ import annotations

import json
from typing import Any

from agentline.errors import PermanentError
from agentline.registry import RunRequest, RunResult
from agentline.runners.common import (
    base_env,
    declared_env,
    detect_missing_tools,
    run_process,
    workspace_exec,
)


def command_argv(cmd: Any) -> list[str]:
    if isinstance(cmd, str) and cmd.strip():
        return ["sh", "-c", cmd]
    if isinstance(cmd, list) and cmd and all(isinstance(x, str | int | float) for x in cmd):
        return [str(x) for x in cmd]
    raise PermanentError('shell runner needs `with: { cmd: [...] }` or `cmd: "..."`')


class ShellRunner:
    name = "shell"

    async def run(self, request: RunRequest) -> RunResult:
        p = request.params
        argv = command_argv(p.get("cmd"))
        extra = {str(k): str(v) for k, v in (p.get("env") or {}).items()}
        env = {
            **declared_env(p.get("pass_env") or ()),
            **extra,
            "AGENTLINE_PROMPT": request.prompt,
            "AGENTLINE_RUN_ID": request.run_id,
            "AGENTLINE_NODE_ID": request.node_id,
            "AGENTLINE_ITERATION": str(request.iteration),
            "AGENTLINE_INPUTS": json.dumps(request.inputs, default=str),
        }
        timeout = request.limits.timeout
        try:
            if request.workspace is not None:
                code, out, err = await workspace_exec(
                    request.workspace, argv, env=env, timeout=timeout, input=request.prompt
                )
            else:
                code, out, err = await run_process(
                    argv, env={**base_env(), **env}, input=request.prompt, timeout=timeout
                )
        except FileNotFoundError:
            return RunResult(output=None, missing_tools=[argv[0]], meta={"exit_code": 127})
        transcript = out + (f"\n--- stderr ---\n{err}" if err else "")
        meta: dict[str, Any] = {"exit_code": code}
        if code in (126, 127):
            missing = detect_missing_tools(err, out)
            if missing:
                return RunResult(None, transcript, missing_tools=missing, meta=meta)
        if code != 0 and not p.get("allow_failure"):
            tail = (err or out).strip()[-500:]
            raise RuntimeError(f"shell runner: {argv[0]} exited {code}: {tail}")
        try:
            output: Any = json.loads(out) if out.strip() else None
        except json.JSONDecodeError:
            output = None
        if output is None:
            output = {"stdout": out, "exit_code": code}
        return RunResult(output=output, transcript=transcript, meta=meta)
