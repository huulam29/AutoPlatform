"""Runner `claude-code`: headless Claude Code (`claude -p ... --output-format json`).

`with:` params:
- `bin`: the CLI (else `$AGENTLINE_CLAUDE_BIN`, else `claude`).
- `model`, `append_system_prompt`, `max_turns` (fallback when the node has none; default 30).
- `budget_flag: true`: also pass `--max-budget-usd <remaining budget>` (only newer CLIs
  have it). Without it the budget is enforced after the run from `total_cost_usd`: the engine
  records the cost and `spend()` sends the node to needs-review if it went over.
- `env`: names of host env vars to pass (default `[ANTHROPIC_API_KEY]`). Nothing else from
  the host env reaches the CLI except PATH/HOME/locale basics when it runs locally.
- `extra_args`: extra CLI arguments (list of strings).

Runs inside the workspace (`workspace.exec`, i.e. in the container) when there is one, else
locally. Tool names map from agentline's (`read`, `edit`, `bash`, `web`) to Claude Code's.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from agentline.errors import BudgetExceeded, PermanentError
from agentline.mcp import claude_config as mcp_config
from agentline.registry import RunRequest, RunResult
from agentline.runners.common import (
    RunnerError,
    base_env,
    child_env,
    detect_missing_tools,
    parse_json_text,
    run_process,
    workspace_exec,
)

TOOL_MAP: dict[str, tuple[str, ...]] = {
    "read": ("Read", "Grep", "Glob"),
    "edit": ("Edit", "Write", "MultiEdit"),
    "bash": ("Bash",),
    "web": ("WebFetch", "WebSearch"),
}
DEFAULT_MAX_TURNS = 30
DEFAULT_ENV = ("ANTHROPIC_API_KEY",)


def map_tools(names: tuple[str, ...] | list[str]) -> list[str]:
    """agentline tool names -> Claude Code tool names (unknown names pass through)."""
    out: list[str] = []
    for n in names:
        for t in TOOL_MAP.get(n, (n,)):
            if t not in out:
                out.append(t)
    return out


def claude_bin(params: dict[str, Any]) -> str:
    return str(params.get("bin") or os.environ.get("AGENTLINE_CLAUDE_BIN") or "claude")


def build_argv(request: RunRequest, mcp_config: str | None = None) -> list[str]:
    p = request.params
    turns = request.limits.max_turns or p.get("max_turns") or DEFAULT_MAX_TURNS
    argv = [
        claude_bin(p),
        "-p",
        request.prompt,
        "--output-format",
        "json",
        "--max-turns",
        str(int(turns)),
    ]
    tools = map_tools(request.limits.allowed_tools)
    servers = p.get("mcp_servers") or {}
    if tools and servers:  # an allowlist must name the node's MCP servers too
        tools += [f"mcp__{name}" for name in servers]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]
    if mcp_config:  # only the node's servers, none from user/project config
        argv += ["--mcp-config", mcp_config, "--strict-mcp-config"]
    if p.get("model"):
        argv += ["--model", str(p["model"])]
    if p.get("append_system_prompt"):
        argv += ["--append-system-prompt", str(p["append_system_prompt"])]
    if p.get("budget_flag") and request.limits.budget_usd is not None:
        argv += ["--max-budget-usd", f"{request.limits.budget_usd:.4f}"]
    argv += [str(x) for x in p.get("extra_args") or ()]
    return argv


def parse_result(stdout: str) -> dict[str, Any] | None:
    """The CLI's final `result` object (a single JSON object, or the last element of a JSON
    message list in verbose mode, or the last JSON line)."""
    candidates = [stdout.strip()] + [ln for ln in reversed(stdout.strip().splitlines())]
    for c in candidates:
        try:
            data = json.loads(c)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            data = next(
                (m for m in reversed(data) if isinstance(m, dict) and m.get("type") == "result"),
                None,
            )
        if isinstance(data, dict) and ("result" in data or data.get("type") == "result"):
            return data
    return None


def write_mcp_config(request: RunRequest) -> tuple[Path | None, str | None]:
    """The node's MCP servers as a private (0600) --mcp-config file, deleted after the run:
    inside the workspace when there is one (a container sees it at its mount), else a temp
    file. Returns (host path, path as the CLI sees it)."""
    servers = request.params.get("mcp_servers") or {}
    if not servers:
        return None, None
    text = json.dumps(mcp_config(servers))
    name = f"agentline-mcp-{secrets.token_hex(6)}.json"
    ws = request.workspace
    if ws is not None:
        host_dir = Path(getattr(ws, "host_path", ws.path)) / ".claude"
        host_dir.mkdir(parents=True, exist_ok=True)
        host = host_dir / name
        inside = str(Path(ws.path) / ".claude" / name)
    else:
        fd, tmp = tempfile.mkstemp(prefix="agentline-mcp-", suffix=".json")
        os.close(fd)
        host = Path(tmp)
        inside = tmp
    fd = os.open(host, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return host, inside


class ClaudeCodeRunner:
    name = "claude-code"

    async def run(self, request: RunRequest) -> RunResult:
        budget = request.limits.budget_usd
        if budget is not None and budget <= 0:
            raise BudgetExceeded(0.0, 0.0, "call")
        p = request.params
        host_file, inside = write_mcp_config(request)
        argv = build_argv(request, inside)
        env = child_env(p, DEFAULT_ENV)
        timeout = request.limits.timeout
        try:
            if request.workspace is not None:
                code, out, err = await workspace_exec(
                    request.workspace, argv, env=env, timeout=timeout
                )
            else:
                code, out, err = await run_process(argv, env={**base_env(), **env}, timeout=timeout)
        except FileNotFoundError:
            raise PermanentError(f"claude-code runner: binary {argv[0]!r} not found") from None
        finally:
            if host_file is not None:
                host_file.unlink(missing_ok=True)
        if code == 127 and request.workspace is not None and "not found" in (err + out):
            return RunResult(None, err, missing_tools=[os.path.basename(argv[0])])

        data = parse_result(out)
        if data is None:
            tail = (err or out).strip()[-500:]
            raise RunnerError(f"claude-code exited {code} without a JSON result: {tail}")
        result_text = str(data.get("result") or "")
        cost = float(data.get("total_cost_usd") or data.get("cost_usd") or 0.0)
        meta: dict[str, Any] = {
            "turns": data.get("num_turns"),
            "session_id": data.get("session_id"),
            "model": p.get("model") or _model_from(data),
            "subtype": data.get("subtype"),
            "exit_code": code,
        }
        transcript = out + (f"\n--- stderr ---\n{err}" if err.strip() else "")
        missing = detect_missing_tools(result_text)
        if missing:
            return RunResult(None, transcript, cost, missing_tools=missing, meta=meta)
        if data.get("is_error") or code != 0:
            raise RunnerError(
                f"claude-code reported an error ({data.get('subtype') or f'exit {code}'}): "
                f"{result_text[-300:]}",
                cost_usd=cost,
                transcript=transcript,
                meta=meta,
            )
        if budget is not None and cost > budget:
            # Without --max-budget-usd the CLI can't stop itself; the engine's spend() of this
            # cost raises BudgetExceeded and the node goes to needs-review.
            meta["budget_exceeded"] = {"cost_usd": cost, "budget_usd": budget}
        ok, value = parse_json_text(result_text)
        return RunResult(
            output=value if ok else result_text, transcript=transcript, cost_usd=cost, meta=meta
        )


def _model_from(data: dict[str, Any]) -> str | None:
    usage = data.get("modelUsage")
    if isinstance(usage, dict) and usage:
        return next(iter(usage))
    return None
